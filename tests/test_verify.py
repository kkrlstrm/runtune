"""`runtune verify`: the executed check between stage and apply.

No model is called here. A fake launcher stands in for `claude -p`: it writes a stream-json
trace whose behavior depends on whether the treatment's skill exists in the run's project,
which is exactly the difference verify measures.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtune import verify  # noqa: E402
from runtune.derive import capabilities, constraints  # noqa: E402
from runtune.lifecycle import ledger, promoter  # noqa: E402
from runtune.lifecycle.store import Workspace  # noqa: E402
from runtune.record import turns  # noqa: E402
from runtune.verify.hosts import claude as claude_host  # noqa: E402
from runtune.verify import hook, route_eval, sandbox, seed, shim, stats, trace  # noqa: E402
from tests import fixtures as fx  # noqa: E402

SKILL = "use-toolkit-cli"


def fake_launcher(behavior="adopts", correct=True):
    """A stand-in host. With the skill present it calls the CLI (if `behavior` says so);
    without it, it writes an inline program — the habit the capability exists to change."""
    def launch(cmd, cwd, env, run_dir, timeout):
        has_skill = os.path.exists(os.path.join(cwd, ".claude", "skills", SKILL, "SKILL.md"))
        use_cli = has_skill and behavior == "adopts"
        command = ("scripts/toolkit db master 'select count(*)'" if use_cli else
                   "python3 -c \"from toolkit import db; print(db.query_master('select count(*)'))\"")
        events = [
            {"type": "system", "subtype": "init", "model": "claude-test", "mcp_servers": [],
             "skills": [SKILL] if has_skill else []},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "42"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
             "total_cost_usd": 0.01, "result": "There are 42 rows." if correct else "I could not tell."},
        ]
        with open(os.path.join(run_dir, "trace.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(e) for e in events) + "\n")
        return 0, None
    return launch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.root, "scripts"))
        with open(os.path.join(self.root, "README.md"), "w") as f:
            f.write("# repo\n")
        self.old_work = sandbox.WORK
        sandbox.WORK = os.path.join(self.tmp.name, "work")
        self.ws = Workspace(os.path.join(self.root, ".runtune"))
        caps, _ = capabilities.derive_inline(fx.inline_ttt())
        cons, _ = constraints.derive(fx.failing_psql())
        self.ws.save_run({"run_id": "r1", "candidates": [c.to_dict() for c in caps + cons], "withheld": []})
        self.cap = caps[0].id
        self.con = cons[0].id

    def tearDown(self):
        sandbox.WORK = self.old_work
        self.tmp.cleanup()

    def make_cli(self):
        p = os.path.join(self.root, "scripts", "toolkit")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho 42\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)

    def stage_and_tasks(self, runs=4, measure=True):
        promoter.stage(self.ws, self.cap, self.root)
        path = verify.init_tasks(self.ws, self.cap)
        doc = json.load(open(path))
        doc["tasks"] = [{"name": "count", "prompt": "How many rows are in the table?",
                         "expect": {"contains": "42"}}]
        doc["runs"] = runs
        if not measure:
            doc["measure"] = None
        json.dump(doc, open(path, "w"))
        return path

    def tree(self):
        out = {}
        for base, dirs, files in os.walk(self.root):
            if ".runtune" in base.split(os.sep):
                continue
            for fn in files:
                p = os.path.join(base, fn)
                out[os.path.relpath(p, self.root)] = open(p, "rb").read()
        return out


class Stats(unittest.TestCase):
    def test_fisher_and_power(self):
        self.assertAlmostEqual(stats.fisher(3, 3, 0, 3), 0.10)
        self.assertAlmostEqual(stats.fisher(4, 4, 0, 4), 0.0286, places=3)
        self.assertEqual(stats.fisher(0, 0, 1, 3), 1.0)
        self.assertAlmostEqual(stats.best_p(3), 0.10)
        self.assertEqual(stats.runs_for(0.05), 4)       # the cli-skill-ab run count could not reach it


class Static(Base):
    def test_skill_naming_a_missing_command_fails(self):
        """RunTune once generated a skill that named a command path that did not exist."""
        promoter.stage(self.ws, self.cap, self.root)
        st = verify.static(self.ws, self.cap, self.root)
        self.assertFalse(st["ok"])
        self.assertTrue(any("does not exist" in c["detail"] for c in st["checks"]))

    def test_existing_command_passes(self):
        self.make_cli()
        promoter.stage(self.ws, self.cap, self.root)
        self.assertTrue(verify.static(self.ws, self.cap, self.root)["ok"])

    def test_constraint_is_not_verify_territory(self):
        promoter.stage(self.ws, self.con, self.root)
        st = verify.static(self.ws, self.con, self.root)
        self.assertFalse(st["ok"])
        self.assertIn("replay gate", st["checks"][0]["detail"])

    def test_grant_revision_cannot_widen(self):
        text = "---\nname: a\ntools: Bash, Read\n---\n"
        self.assertIn("tools: Bash", sandbox.narrow_grant(text, ["Bash"]))
        with self.assertRaises(ValueError):
            sandbox.narrow_grant(text, ["Bash", "WebFetch"])


class Tasks(Base):
    def test_placeholders_refused(self):
        promoter.stage(self.ws, self.cap, self.root)
        path = verify.init_tasks(self.ws, self.cap)
        with self.assertRaises(verify.VerifyError):
            verify.load_tasks(path, self.cap)
        with self.assertRaises(verify.VerifyError):
            verify.init_tasks(self.ws, self.cap)          # never overwrites

    def test_default_measure_targets_cli_and_inline(self):
        promoter.stage(self.ws, self.cap, self.root)
        doc = json.load(open(verify.init_tasks(self.ws, self.cap)))
        self.assertIn("toolkit", doc["measure"]["new"]["bash"])
        self.assertIn("toolkit", doc["measure"]["old"]["bash"])
        self.assertEqual(doc["repo_modules"], ["toolkit"])


class Drafted(Base):
    """`--init` drafts tasks from the sessions behind the evidence, and holds them for review."""

    def transcript(self, ref, prompt="How many orders came in last week?", output="count\n137\n",
                   answer="137 orders came in last week."):
        d = os.path.join(self.tmp.name, "claude-projects", "-repo")
        os.makedirs(d, exist_ok=True)
        rows = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": [
                {"type": "text", "text": "<ide_selection>x</ide_selection>"}, {"type": "text", "text": prompt}]}},
            {"type": "assistant", "timestamp": ref["ts"], "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": ref["text"]}}]}},
            {"type": "user", "timestamp": ref["ts"], "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": output}]}},
            {"type": "assistant", "timestamp": ref["ts"], "message": {"content": [{"type": "text", "text": answer}]}},
        ]
        with open(os.path.join(d, f"{ref['session']}.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows))
        return os.path.dirname(d)

    def setUp(self):
        super().setUp()
        self.old_hosts = dict(turns.HOSTS)

    def tearDown(self):
        turns.HOSTS.clear()
        turns.HOSTS.update(self.old_hosts)
        super().tearDown()

    def test_samples_carry_a_session(self):
        promoter.stage(self.ws, self.cap, self.root)
        samples = self.ws.get(self.cap)["candidate"]["samples"]
        self.assertTrue(samples and all(s["session"] and s["actor"] == "root" for s in samples))
        self.assertEqual(len({s["session"] for s in samples}), len(samples))

    def test_init_drafts_a_reviewable_task(self):
        promoter.stage(self.ws, self.cap, self.root)
        ref = self.ws.get(self.cap)["candidate"]["samples"][0]
        root = self.transcript(ref)
        turns.HOSTS["claude"] = lambda s: turns._claude(s, root=root)
        path = verify.init_tasks(self.ws, self.cap)
        doc = json.load(open(path))
        t = doc["tasks"][0]
        self.assertEqual(t["prompt"], "How many orders came in last week?")   # the injected block is gone
        self.assertFalse(t["reviewed"])
        self.assertTrue(re.search(t["expect"]["regex"], "There were 137."))
        self.assertIn("137", t["from_evidence"]["output"])
        with self.assertRaises(verify.VerifyError) as cm:
            verify.load_tasks(path, self.cap)
        self.assertIn("not reviewed", str(cm.exception))
        t["reviewed"] = True
        json.dump(doc, open(path, "w"))
        self.assertEqual(verify.load_tasks(path, self.cap)["tasks"][0]["name"], "seen-1")

    def test_a_drafted_prompt_may_contain_angle_brackets(self):
        promoter.stage(self.ws, self.cap, self.root)
        ref = self.ws.get(self.cap)["candidate"]["samples"][0]
        root = self.transcript(ref, prompt="Count rows where tag = <none>")
        turns.HOSTS["claude"] = lambda s: turns._claude(s, root=root)
        path = verify.init_tasks(self.ws, self.cap)
        doc = json.load(open(path))
        doc["tasks"] = [dict(t, reviewed=True) for t in doc["tasks"]]
        json.dump(doc, open(path, "w"))
        verify.load_tasks(path, self.cap)

    def test_no_transcript_leaves_the_scaffold(self):
        promoter.stage(self.ws, self.cap, self.root)
        turns.HOSTS["claude"] = lambda s: []
        doc = json.load(open(verify.init_tasks(self.ws, self.cap)))
        self.assertEqual(doc["tasks"][0]["name"], "<short-name>")
        self.assertTrue(doc["_drafting_notes"])

    def test_expectation(self):
        self.assertIsNotNone(seed.expectation("How many?", "n\n1,234\n", "There are 1234 rows."))
        self.assertIsNone(seed.expectation("How many in 2026?", "2026\n", "In 2026 there were none."))
        self.assertIsNone(seed.expectation("Count rows over 500", "500", "Rows over 500: none"))
        rx = seed.expectation("?", "137", "137 orders")["regex"]
        self.assertTrue(re.search(rx, "we got 137 orders"))
        self.assertFalse(re.search(rx, "we got 1370 orders"))

    def test_request_strips_injected_context(self):
        self.assertEqual(turns.request("<environment_context>a</environment_context>\nfix it"), "fix it")
        self.assertEqual(turns.request("<command-name>/compact</command-name>"), "")
        self.assertEqual(turns.request("# AGENTS.md instructions for /x\n..."), "")


class Runs(Base):
    def test_effect_passes_and_apply_accepts_the_result(self):
        self.make_cli()
        self.stage_and_tasks(runs=4)
        before = self.tree()
        res = verify.run(self.ws, self.cap, self.root, launcher=fake_launcher(), log=lambda *_: None)
        self.assertEqual(res["verdict"], "pass", res["reasons"])
        self.assertEqual(res["arms"]["treatment"]["adopted"], 4)
        self.assertEqual(res["arms"]["control"]["adopted"], 0)
        self.assertEqual(self.tree(), before, "verify must not change the real repository")
        art = promoter.apply(self.ws, self.cap, "alice", eval_ref=res["path"])
        self.assertEqual(art["state"], "active")

    def test_three_runs_cannot_pass(self):
        """3/3 vs 0/3 is p = 0.10: an honest 'inconclusive', not a pass."""
        self.make_cli()
        self.stage_and_tasks(runs=3)
        res = verify.run(self.ws, self.cap, self.root, launcher=fake_launcher(), log=lambda *_: None)
        self.assertEqual(res["verdict"], "inconclusive")

    def test_skill_agents_ignore_is_no_effect(self):
        self.make_cli()
        self.stage_and_tasks(runs=4)
        res = verify.run(self.ws, self.cap, self.root, launcher=fake_launcher("ignores"), log=lambda *_: None)
        self.assertEqual(res["verdict"], "no-effect")
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.cap, "alice", eval_ref=res["path"])

    def test_static_failure_spends_nothing(self):
        self.stage_and_tasks(runs=4)                      # no scripts/toolkit
        calls = []
        res = verify.run(self.ws, self.cap, self.root, log=lambda *_: None,
                         launcher=lambda *a: calls.append(a) or (0, None))
        self.assertEqual(res["verdict"], "fail")
        self.assertEqual(calls, [])

    def test_errors_are_never_counted_as_runs(self):
        self.make_cli()
        self.stage_and_tasks(runs=4)
        res = verify.run(self.ws, self.cap, self.root, log=lambda *_: None,
                         launcher=lambda cmd, cwd, env, d, t: (1, "timed out after 1s"))
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertEqual(res["arms"]["treatment"]["valid"], 0)


class ApplyGate(Base):
    def setUp(self):
        super().setUp()
        self.make_cli()
        self.stage_and_tasks(runs=4)
        self.res = verify.run(self.ws, self.cap, self.root, launcher=fake_launcher(), log=lambda *_: None)

    def test_result_for_another_draft_refused(self):
        art = self.ws.get(self.cap)
        art["candidate"]["proposal"]["skill_md"] += "\nAlso run rm -rf.\n"
        self.ws.put(art)
        with self.assertRaisesRegex(promoter.Refused, "draft changed"):
            promoter.apply(self.ws, self.cap, "alice", eval_ref=self.res["path"])

    def test_any_file_is_not_an_eval(self):
        bogus = os.path.join(self.tmp.name, "hosts")
        open(bogus, "w").write("127.0.0.1 localhost\n")
        with self.assertRaisesRegex(promoter.Refused, "not a readable verify result"):
            promoter.apply(self.ws, self.cap, "alice", eval_ref=bogus)

    def test_required_by_authority(self):
        auth = self.ws.authority()
        auth["require_verify"] = ["capability"]
        json.dump(auth, open(os.path.join(self.ws.root, "authority.json"), "w"))
        with self.assertRaisesRegex(promoter.Refused, "requires a passing"):
            promoter.apply(self.ws, self.cap, "alice")
        promoter.apply(self.ws, self.cap, "alice", eval_ref=self.res["path"])

    def test_not_required_by_default(self):
        promoter.apply(self.ws, self.cap, "alice")

    def test_verify_is_ledgered(self):
        rows = ledger.read(self.ws.ledger_path)
        self.assertTrue(any(r.get("action") == "verify" and r.get("verdict") == "pass" for r in rows))
        self.assertTrue(ledger.verify(self.ws.ledger_path)[0])


class RouteEval(unittest.TestCase):
    """`apply --eval` for a route: the eval must be about this mode, candidate and incumbent."""
    REQ = {"mode": "extract", "candidate": "qwen/cheap", "incumbent": "deepseek/v4-flash"}

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def _check(self, req=None, **over):
        from datetime import datetime, timezone
        doc = {"schema": route_eval.SCHEMA, "mode": "extract", "candidate": "qwen/cheap:free",
               "incumbent": "deepseek/v4-flash", "verdict": "pass", "cases": 40,
               "candidate_score": 0.91, "incumbent_score": 0.90,
               "created": datetime.now(timezone.utc).isoformat()}
        doc.update(over)
        doc = {k: v for k, v in doc.items() if v is not None}
        p = os.path.join(self.d, "eval.json")
        json.dump(doc, open(p, "w"))
        return route_eval.check(req or self.REQ, p)

    def test_a_matching_passing_eval_is_usable(self):
        self.assertEqual(self._check(), [])

    def test_each_mismatch_is_refused(self):
        for over, why in (({"mode": "classify"}, "mode `classify`"),
                          ({"candidate": "other/model"}, "not the candidate"),
                          ({"incumbent": "gpt/old"}, "not the model this replaces"),
                          ({"verdict": "fail"}, "not `pass`"),
                          ({"cases": 5}, "at least 20"),
                          ({"cases": True}, "at least 20"),
                          ({"incumbent_score": None}, "incumbent_score"),
                          ({"candidate_score": 0.80}, "below the incumbent"),
                          ({"created": "2026-01-01T00:00:00Z"}, "days old"),
                          ({"created": "yesterday"}, "not an ISO timestamp")):
            with self.subTest(over=over):
                problems = self._check(**over)
                self.assertTrue(any(why in p for p in problems), problems)

    def test_a_declared_margin_allows_a_slightly_lower_score(self):
        self.assertEqual(self._check(candidate_score=0.89, non_inferiority_margin=0.02), [])

    def test_not_an_eval(self):
        p = os.path.join(self.d, "x.json")
        open(p, "w").write("{}")
        self.assertIn("not a route eval result", route_eval.check(self.REQ, p)[0])
        self.assertIn("not a readable", route_eval.check(self.REQ, p + ".missing")[0])

    def test_revalidation_needs_no_incumbent(self):
        req = {"mode": "extract", "candidate": "qwen/cheap", "incumbent": None}
        self.assertEqual(self._check(req, incumbent=None, incumbent_score=None), [])


class StaticFailure(Base):
    def test_a_failed_static_check_is_a_verdict_not_a_crash(self):
        promoter.stage(self.ws, self.cap, self.root)        # no CLI: static fails
        path = verify.init_tasks(self.ws, self.cap)
        doc = json.load(open(path))
        doc["tasks"] = [{"name": "t", "prompt": "p", "expect": {"contains": "x"}}]
        json.dump(doc, open(path, "w"))
        art = self.ws.get(self.cap)
        art["target"] = "/elsewhere/.claude/skills/x"      # outside the root as well
        self.ws.put(art)
        res = verify.run(self.ws, self.cap, self.root, launcher=fake_launcher(), log=lambda *_: None)
        self.assertEqual(res["verdict"], "fail")


class Hosts(unittest.TestCase):
    """Each host's event stream reads into the same record, with tool calls renamed so that a
    measure means the same thing everywhere. Event lines follow each host's documented format."""

    def write(self, lines):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "state"))
        with open(os.path.join(d, "trace.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines))
        return d

    def test_codex(self):
        from runtune.verify.hosts import codex
        d = self.write([
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                                "command": "/bin/zsh -lc 'cat .agents/skills/use-toolkit/SKILL.md'",
                                                "aggregated_output": "Run scripts/toolkit", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "i2", "type": "command_execution",
                                                "command": "bash -lc \"scripts/toolkit db 'select 1'\"",
                                                "aggregated_output": "42\n", "exit_code": 0}},
            {"type": "item.completed", "item": {"id": "i3", "type": "file_change",
                                                "changes": [{"path": "/w/proj/n.md", "kind": "add"}]}},
            {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "There are 42."}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}}])
        r = codex.parse(d)
        self.assertTrue(r.started and r.finished)
        self.assertEqual(r.bash[1], "scripts/toolkit db 'select 1'")          # the shell wrapper is removed
        self.assertEqual(trace.matches(r, {"bash": r"scripts/toolkit db"}), 1)
        self.assertEqual(trace.matches(r, {"skill": "use-toolkit"}), 1)      # read of SKILL.md counts as use
        self.assertEqual(r.answer, "There are 42.")
        self.assertEqual(r.tokens["output_tokens"], 3)
        self.assertTrue(any(u["name"] == "Write" for u in r.uses))
        d = self.write([{"type": "thread.started"}, {"type": "turn.failed", "error": {"message": "quota"}}])
        r = codex.parse(d)
        self.assertFalse(r.finished)
        self.assertEqual(r.end_reason, "quota")

    def test_codex_skill_list_keeps_project_skills_only(self):
        from runtune.verify.hosts import codex
        text = ("- `r0` = `/w/codexhome/skills/.system`\n- `r1` = `/w/proj/.agents/skills`\n"
                "- imagegen: x (file: r0/imagegen/SKILL.md)\n- use-toolkit: y (file: r1/use-toolkit/SKILL.md)")
        self.assertEqual(codex.skills_in_prompt(text), ["use-toolkit"])

    def test_codex_config_pins_the_real_repo_read_only(self):
        from runtune.verify.hosts import codex
        d = tempfile.mkdtemp()
        open(os.path.join(d, ".env"), "w").write("X=1")
        toml = codex.config_toml("/w/run", d, None, "/home/u")
        self.assertIn(f'"{d}" = "read"', toml)
        self.assertIn(f'"{d}/.env" = "deny"', toml)
        self.assertIn('"/home/u/.codex" = "deny"', toml)
        self.assertIn("allow_login_shell = false", toml)
        self.assertNotIn("network_proxy", toml)
        live = codex.config_toml("/w/run", d, {"network": ["api.example.com"]}, "/home/u")
        self.assertIn('"api.example.com" = "allow"', live)

    def test_cursor(self):
        from runtune.verify.hosts import cursor
        d = self.write([
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s"},
            {"type": "tool_call", "subtype": "started", "call_id": "c1",
             "tool_call": {"readToolCall": {"args": {"path": ".cursor/skills/use-toolkit/SKILL.md"}}}},
            {"type": "tool_call", "subtype": "completed", "call_id": "c1",
             "tool_call": {"readToolCall": {"args": {"path": ".cursor/skills/use-toolkit/SKILL.md"},
                                            "result": {"success": {"content": "Run scripts/toolkit"}}}}},
            {"type": "tool_call", "subtype": "completed", "call_id": "c2",
             "tool_call": {"shellToolCall": {"args": {"command": "scripts/toolkit db 'select 1'"},
                                             "result": {"success": {"stdout": "42", "exitCode": 0}}}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "42 rows"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "There are 42 rows.",
             "usage": {"inputTokens": 5, "outputTokens": 2}}])
        r = cursor.parse(d, cursor.Cursor.skill_dirs)
        self.assertTrue(r.started and r.finished)
        self.assertEqual(trace.matches(r, {"bash": "toolkit db"}), 1)
        self.assertEqual(trace.matches(r, {"skill": "use-toolkit"}), 1)
        self.assertEqual(r.results["c2"], "42")
        self.assertEqual(r.answer, "There are 42 rows.")

    def test_antigravity(self):
        from runtune.verify.hosts import antigravity
        d = self.write([
            {"event": "init", "conversation_id": "c", "init": {"model": "gemini-x", "cwd": "/w"}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "ACTIVE", "step_type": "tool",
                                                     "tool_info": {"name": "run_command"}}},
            {"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "tool",
                                                     "tool_info": {"name": "run_command",
                                                                   "parameters": {"CommandLine": '"scripts/toolkit db"'},
                                                                   "output": "The command exited with code 0.\n42"}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "42", "num_turns": 1,
                                           "usage": {"input_tokens": 9}}}])
        r = antigravity.parse(d)
        self.assertTrue(r.finished)
        self.assertEqual(r.bash, ["scripts/toolkit db"])
        self.assertIn("42", r.results["step-1"])
        d = self.write([{"event": "init", "init": {}}, {"event": "result", "result": {"status": "SUCCESS", "response": ""}}])
        r = antigravity.parse(d)
        self.assertFalse(r.finished)                     # SUCCESS with nothing in it is not a finished run
        self.assertIn("empty", r.end_reason)

    def test_antigravity_falls_back_to_the_run_transcript(self):
        """agy has returned SUCCESS with an empty response; the run's own transcript still has it."""
        from runtune.verify.hosts import antigravity
        d = self.write([{"event": "init", "init": {}}, {"event": "result", "result": {"status": "SUCCESS", "response": ""}}])
        logs = os.path.join(d, "home", ".gemini", "antigravity", "brain", "conv1", ".system_generated", "logs")
        os.makedirs(logs)
        rows = [{"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>count orders</USER_REQUEST>"},
                {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
                 "tool_calls": [{"name": "run_command", "args": {"CommandLine": "scripts/toolkit db"}}]},
                {"step_index": 2, "source": "MODEL", "type": "GENERIC", "content": "The command exited with code 0.\n137"},
                {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "137 orders."}]
        with open(os.path.join(logs, "transcript.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in rows))
        r = antigravity.parse(d)
        self.assertTrue(r.finished)
        self.assertEqual(r.answer, "137 orders.")
        self.assertEqual(r.bash, ["scripts/toolkit db"])
        self.assertIn("137", r.results[r.uses[0]["id"]])

    def test_project_files_that_widen_a_run_are_removed_from_its_copy(self):
        from runtune.verify import hosts as H
        repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(repo, ".cursor"))
        for f in ("sandbox.json", "mcp.json", "rules.md"):
            open(os.path.join(repo, ".cursor", f), "w").write("{}")
        old = sandbox.WORK
        sandbox.WORK = tempfile.mkdtemp()
        try:
            kit = sandbox.prepare(os.path.join(sandbox.WORK, "run"), repo, {}, {"mocks": []},
                                  {"python3": sys.executable}, H.get("cursor"))
        finally:
            sandbox.WORK = old
        self.assertEqual(sorted(os.listdir(os.path.join(kit["proj"], ".cursor"))), ["rules.md"])
        self.assertTrue(os.path.exists(os.path.join(repo, ".cursor", "sandbox.json")))   # the real repo is untouched
        self.assertTrue(any("sandbox.json" in x for x in kit["limits"]))

    def test_static_says_when_the_host_would_never_load_the_target(self):
        from runtune.verify import hosts as H
        self.assertTrue(H.get("codex").reads(".agents/skills/x"))
        self.assertFalse(H.get("codex").reads(".claude/skills/x"))
        self.assertTrue(H.get("cursor").reads(".claude/skills/x"))

    def test_hosts_without_a_passing_selftest_are_refused(self):
        from runtune.verify import hosts as H, selftest
        old = sandbox.WORK
        sandbox.WORK = tempfile.mkdtemp()
        try:
            h = H.get("codex")
            h.version = lambda: "codex 1.0"
            self.assertFalse(selftest.passed(h)[0])
            os.makedirs(os.path.join(sandbox.WORK, "selftest"))
            json.dump({"version": "codex 1.0", "status": "pass"}, open(selftest.record_path("codex"), "w"))
            self.assertTrue(selftest.passed(h)[0])
            h.version = lambda: "codex 1.1"                 # an upgrade needs a new selftest
            self.assertIn("installed is codex 1.1", selftest.passed(h)[1])
        finally:
            sandbox.WORK = old

    def test_hook_reads_antigravity_and_cursor_shell_payloads(self):
        c = Containment()
        _, p = c.run_hook("antigravity", {"toolCall": {"name": "write_to_file", "args": {"TargetFile": '"/etc/x"'}},
                                          "workspacePaths": ["/"]})
        self.assertEqual(json.loads(p.stdout)["decision"], "deny")
        _, p = c.run_hook("cursor", {"command": "PATH=/usr/bin python3 x.py", "cwd": "/"})
        self.assertEqual(json.loads(p.stdout)["permission"], "deny")


class Containment(unittest.TestCase):
    def test_hook_resolves_writes_through_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            repo, run = os.path.join(d, "repo"), os.path.join(d, "run")
            os.makedirs(os.path.join(repo, "src"))
            os.makedirs(os.path.join(run, "proj"))
            os.symlink(os.path.join(repo, "src"), os.path.join(run, "proj", "src"))
            self.assertTrue(hook.inside_run(os.path.join(run, "proj", "notes.md"), run))
            self.assertTrue(hook.inside_run(os.path.join(run, "proj", "new", "x.txt"), run))
            self.assertFalse(hook.inside_run(os.path.join(run, "proj", "src", "evil.py"), run))
            self.assertFalse(hook.inside_run(os.path.join(repo, "x"), run))

    def test_project_is_a_clone_with_real_directories(self):
        """`find dir -type f` does not descend into a symlinked dir: a farm of links showed one
        run an empty project (2026-10-08). The default project must be real directories."""
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, "repo")
            os.makedirs(os.path.join(repo, "pkg"))
            open(os.path.join(repo, "pkg", "m.py"), "w").write("x = 1\n")
            open(os.path.join(repo, ".env"), "w").write("SECRET=1\n")
            proj = os.path.join(d, "proj")
            mode = sandbox.build_farm(proj, repo, {"pkg/new.py": "y = 2\n"})
            if mode == "clone":
                self.assertFalse(os.path.islink(os.path.join(proj, "pkg")))
            self.assertFalse(os.path.lexists(os.path.join(proj, ".env")))
            self.assertTrue(os.path.exists(os.path.join(proj, "pkg", "new.py")))
            self.assertFalse(os.path.exists(os.path.join(repo, "pkg", "new.py")), "overlay leaked into the repo")

    def run_hook(self, host, event):
        """Run the copied hook as a host would. `event` may be a function of the run dir."""
        import shutil as sh
        import subprocess
        run = tempfile.mkdtemp()
        state = os.path.join(run, "state")
        os.makedirs(os.path.join(run, "proj"))
        os.makedirs(state)
        sh.copy(hook.__file__, os.path.join(state, "hook.py"))
        ev = event(run) if callable(event) else event
        p = subprocess.run([sys.executable, os.path.join(state, "hook.py"), host], input=json.dumps(ev),
                           capture_output=True, text=True, env={**os.environ, "RUNTUNE_VERIFY_STATE": state})
        return run, p

    def test_hook_reads_paths_inside_a_codex_patch(self):
        patch = "*** Begin Patch\n*** Update File: {f}\n@@\n-a\n+b\n*** End Patch\n"
        _, p = self.run_hook("codex", {"tool_name": "apply_patch", "cwd": "/",
                                       "tool_input": {"input": patch.format(f="/etc/hosts")}})
        self.assertIn('"deny"', p.stdout)
        _, p = self.run_hook("codex", lambda run: {
            "tool_name": "apply_patch", "cwd": os.path.join(run, "proj"),
            "tool_input": {"input": patch.format(f="notes.md") + "*** Move to: ../../escape.md\n"}})
        self.assertIn('"deny"', p.stdout)                   # a move target counts as a write
        _, p = self.run_hook("codex", lambda run: {
            "tool_name": "apply_patch", "cwd": os.path.join(run, "proj"), "tool_input": {"input": patch.format(f="notes.md")}})
        self.assertEqual(p.stdout, "")                      # inside the run: allowed

    def test_hook_reads_a_patch_sent_through_the_shell(self):
        cmd = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: /tmp/escape.txt\n+x\n*** End Patch\nEOF"
        _, p = self.run_hook("codex", {"tool_name": "Bash", "tool_input": {"command": cmd}})
        self.assertIn('"deny"', p.stdout)

    def test_hook_speaks_cursor(self):
        _, p = self.run_hook("cursor", {"tool_name": "Write", "tool_input": {"path": "/etc/x"}, "cursor_version": "1"})
        self.assertEqual(json.loads(p.stdout)["permission"], "deny")
        _, p = self.run_hook("cursor", {"tool_name": "Shell", "tool_input": {"command": "/usr/bin/python3 -c 1"}})
        self.assertEqual(json.loads(p.stdout)["permission"], "deny")

    def test_hook_fails_closed(self):
        run, p = self.run_hook("claude", {"tool_name": "Write", "tool_input": None, "cwd": 5})
        self.assertIn('"deny"', p.stdout)

    def test_hook_patterns(self):
        for cmd in ("/usr/bin/python3 -c 1", "x=$(/usr/bin/curl -s u)", "cd a && /opt/bin/psql"):
            self.assertTrue(hook.ABS_BIN.search(cmd), cmd)
        for cmd in ("python3 x.py", "ls /opt/homebrew/bin/", "scripts/toolkit db"):
            self.assertFalse(hook.ABS_BIN.search(cmd), cmd)
        self.assertTrue(hook.PATH_TAMPER.search("PATH=/usr/bin python3 x.py"))
        self.assertTrue(hook.PATH_TAMPER.search("env -i python3 x.py"))
        self.assertFalse(hook.PATH_TAMPER.search("echo $PATH"))

    def test_shim_classifies_python(self):
        self.assertEqual(shim.python_target(["-u", "x.py", "a"]), ("script", "x.py", ["a"]))
        self.assertEqual(shim.python_target(["-m", "toolkit", "db"]), ("module", "toolkit", ["db"]))
        self.assertEqual(shim.python_target(["-c", "1"])[0], "inline")

    def test_settings_close_known_holes(self):
        with tempfile.TemporaryDirectory() as d:
            s = claude_host.settings(d, d, {"python3": "/usr/bin/python3"}, d, None)
            sb = s["sandbox"]
            self.assertTrue(sb["network"]["strictAllowlist"])     # bypass mode allows hosts otherwise
            self.assertFalse(sb["allowUnsandboxedCommands"])
            self.assertTrue(sb["failIfUnavailable"])
            self.assertEqual(sb["network"]["allowedDomains"], [])
            self.assertIn(os.path.join(d, ".runtune"), sb["filesystem"]["denyRead"])

    def test_env_is_an_allowlist_and_records_nowhere_real(self):
        os.environ["DATABASE_URL"] = "postgres://secret"
        try:
            env = sandbox.env_for("/r", "/r/bin", None)
            self.assertNotIn("DATABASE_URL", env)
            self.assertTrue(env["RUNTUNE_HOME"].startswith("/r/"))
            self.assertIn("DATABASE_URL", sandbox.env_for("/r", "/r/bin", {"env": ["DATABASE_URL"]}))
        finally:
            del os.environ["DATABASE_URL"]

    def test_peeking_and_real_repo_use_are_flagged(self):
        rec = trace.RunRecord("/w/run")
        rec.uses = [{"id": "1", "name": "Bash", "input": {"command": "cd /w/run/proj/src && ls"}}]
        self.assertEqual(trace.peeked(rec, "/w/run", "/repo"), [])
        for cmd in ("cat /w/run/state/mocks.json", "echo $RUNTUNE_VERIFY_STATE", "cat /repo/.env"):
            rec.uses = [{"id": "1", "name": "Bash", "input": {"command": cmd}}]
            self.assertTrue(trace.peeked(rec, "/w/run", "/repo"), cmd)


if __name__ == "__main__":
    unittest.main()
