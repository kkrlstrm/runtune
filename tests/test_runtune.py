import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from runtune import measure
from runtune.derive import capabilities, constraints, routes, subagents
from runtune.evidence import redact, shapes, tiers
from runtune.evidence.schema import Corpus
from runtune.lifecycle import ledger, promoter
from runtune.lifecycle.store import Workspace
from tests import fixtures as fx


class Shapes(unittest.TestCase):
    def test_head_survives_prefixes(self):
        self.assertEqual(shapes.first_tokens("# note\ncd /x && python3 scripts/a.py --b"), ["python3", "a.py"])
        self.assertEqual(shapes.first_tokens('cd "/a b" && git push'), ["git", "push"])
        self.assertEqual(shapes.first_tokens("X=$(grep foo .env) && echo"), ["grep"])
        self.assertEqual(shapes.first_tokens("python3 <<'EOF'\nimport ttt"), ["python3", "<<"])

    def test_inline_target_names_local_code_only(self):
        self.assertEqual(shapes.inline_target('python3 -c "from ttt import db, clickup; db.x()"'), "ttt:clickup,db")
        self.assertIsNone(shapes.inline_target('python3 -c "import json, sys"'))
        # a quote directly before `from` used to drop the from-clause and mis-key the import
        self.assertEqual(shapes.inline_target("python3 -c \"from ttt import client\""), "ttt:client")

    def test_error_normalization_collapses_urls(self):
        a = shapes.normalize_error("exit 1 [page-digest] https://a.gov/x?id=9 -> tier=dead")
        b = shapes.normalize_error("exit 1 [page-digest] https://b.org/y?did=2 -> tier=dead")
        self.assertEqual(a, b)


class Tiers(unittest.TestCase):
    def test_denominator_required(self):
        self.assertEqual(tiers.classify_failure(3, 3)[0], tiers.ANECDOTAL)
        self.assertEqual(tiers.classify_failure(3, 0)[0], tiers.UNKNOWN)
        self.assertEqual(tiers.classify_failure(19, 20)[0], tiers.DETERMINISTIC)
        self.assertEqual(tiers.classify_failure(3, 300)[0], tiers.PROBABILISTIC)

    def test_unknown_tier_gets_strictest_ceiling(self):
        self.assertEqual(tiers.ceiling("made-up"), "monitor")
        self.assertFalse(tiers.within_ceiling("block", tiers.PROBABILISTIC))

    def test_reuse_is_breadth_not_volume(self):
        self.assertEqual(tiers.classify_reuse(1, 1), tiers.LOCAL)
        self.assertEqual(tiers.classify_reuse(5, 2), tiers.RECURRING)


class Constraints(unittest.TestCase):
    def test_psql_proposed_cross_host_with_replay(self):
        got, held = constraints.derive(fx.failing_psql())
        psql = [c for c in got if "psql" in c.key]
        self.assertEqual(len(psql), 1)
        c = psql[0]
        self.assertEqual(c.tier, tiers.REPRODUCIBLE)
        self.assertEqual(set(c.sources), {"claude", "codex"})
        self.assertEqual(c.numbers["replay"]["matched"], 10)
        self.assertEqual(c.numbers["replay"]["matched_fail"], 6)
        self.assertEqual(c.proposal["ruleset_rule"]["action"], "monitor")

    def test_single_session_retry_loop_is_withheld_with_reason(self):
        got, held = constraints.derive(fx.failing_psql())
        self.assertFalse(any("make" in c.key for c in got))
        loop = [c for c in held if "make" in c.key][0]
        self.assertIn("breadth", [g.name for g in loop.gates if not g.passed])

    def test_replay_ruleset(self):
        rows = constraints.replay_ruleset([{"id": "r", "tool": "Bash", "any": [r"\bpsql\b"], "action": "block"}],
                                          fx.failing_psql())
        self.assertEqual(rows[0]["matched"], 10)
        self.assertTrue(rows[0]["over_ceiling"])   # 60% failing earns deny, not block


class Capabilities(unittest.TestCase):
    def test_inline_program_becomes_adoption_gap_when_cli_exists(self):
        got, _ = capabilities.derive_inline(fx.inline_ttt())
        self.assertEqual(len(got), 1)
        c = got[0]
        self.assertTrue(c.numbers["cli_exists"])
        self.assertIn("db.query_master", dict(c.numbers["verbs_top"]))
        self.assertEqual([r[0] for r in c.ladder if r[1]], ["skill"])
        # the skill names only syntax observed succeeding, never syntax derived from function names
        self.assertIn("`ttt db master …`", c.proposal["skill_md"])
        self.assertNotIn("query-master", c.proposal["skill_md"])

    def test_without_cli_proposes_new_capability(self):
        got, _ = capabilities.derive_inline(fx.inline_ttt(cli_after_day=None))
        self.assertFalse(got[0].numbers["cli_exists"])
        self.assertIn("DRAFT", got[0].proposal["skill_md"])


class Subagents(unittest.TestCase):
    def test_fanout_census_proposes_narrow_type_with_full_census(self):
        got, _ = subagents.derive(fx.fanouts())
        c = [x for x in got if x.key.startswith("claude|workflow-subagent")][0]
        self.assertEqual(sorted(c.proposal["grant"]), ["Bash", "WebSearch"])  # {Bash} folded into superset
        self.assertEqual(c.numbers["fanouts"], 6)

    def test_overgrant_ignores_tools_the_recorder_cannot_see(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-reader.md"), "w") as f:
                f.write("---\nname: db-reader\ntools: Bash, Read, Grep\n---\nReturn status ok or failed.\n")
            got, _ = subagents.derive(fx.fanouts(), agents_dir=d)
            og = [x for x in got if x.key == "overgrant|db-reader"][0]
            self.assertEqual(og.proposal["grant"], ["Bash", "Grep"])   # Read unused; Grep unobservable, kept


class Routes(unittest.TestCase):
    def setUp(self):
        self.got, self.held = routes.derive(fx.routing(), today=datetime(2026, 8, 30).date())
        self.keys = {c.key for c in self.got}

    def test_drift_only_after_clearance(self):
        drift = [c for c in self.got if c.key == "drift|dial|openai/old"][0]
        self.assertEqual(drift.numbers["calls"], 25)
        self.assertIn("preclearance|dial|openai/old", {c.key for c in self.held})

    def test_unapproved_idle_stale_unattributed(self):
        self.assertIn("unapproved|(no mode)|x/rogue", self.keys)
        life = [c for c in self.got if c.key == "lifecycle|idle-mode"][0]
        self.assertTrue(life.numbers["idle"] and life.numbers["stale"])
        self.assertIn("unattributed", self.keys)

    def test_idle_overturned_when_the_bill_shows_the_model_running(self):
        c = fx.routing()
        c.spend += [{"usage_date": (fx.T0 + timedelta(days=d)).date().isoformat(), "model": "qwen/qwen3",
                     "requests": 30, "usage_usd": 0.1} for d in range(3)]
        got, _ = routes.derive(c, today=datetime(2026, 8, 30).date())
        keys = {x.key for x in got}
        self.assertIn("bypass|idle-mode", keys)
        self.assertNotIn("lifecycle|idle-mode", keys)

    def test_challenger_widens_and_requires_eval(self):
        ch = [c for c in self.got if c.key == "challenger|extract|qwen/cheap"][0]
        self.assertEqual(ch.direction, "widen")
        self.assertIn("eval", ch.requires)


class Measure(unittest.TestCase):
    def test_did_and_attempt_shift(self):
        c = Corpus()
        for d in range(20):
            for k in range(4):
                c.events.append(fx.ev(day=d, session=f"s{d}", text="psql x", ok=not (d < 10 and k < 2),
                                      error="conn refused", minute=k))
                c.events.append(fx.ev(day=d, session=f"s{d}", text="git log", ok=True, minute=10 + k))
        c.finalize()
        at = fx.T0 + timedelta(days=10)
        r = measure.before_after(c, lambda e: e.text.startswith("psql"), at, days=10)
        self.assertAlmostEqual(r["before"]["target"]["fail_rate"], 0.5)
        self.assertAlmostEqual(r["after"]["target"]["fail_rate"], 0.0)
        self.assertLess(r["net_change"], 0)

    def test_gap_days_excluded(self):
        c = Corpus()
        for d in list(range(0, 5)) + list(range(15, 20)):
            c.events.append(fx.ev(day=d, text="psql x"))
        c.finalize()
        self.assertTrue(c.coverage["claude"].gaps)
        r = measure.before_after(c, lambda e: True, fx.T0 + timedelta(days=10), days=10)
        self.assertTrue(r["gap_days_excluded"]["claude"])

    def test_one_sources_gap_does_not_erase_another(self):
        c = Corpus()
        for d in range(20):
            c.events.append(fx.ev(day=d, text="psql x"))
        for d in (0, 19):  # codex: silent for 18 days in the middle
            c.events.append(fx.ev(source="codex", surface="exec", day=d, text="ls"))
        c.finalize()
        self.assertTrue(c.coverage["codex"].gaps)
        r = measure.before_after(c, lambda e: e.text.startswith("psql"), fx.T0 + timedelta(days=10), days=10)
        self.assertEqual(r["before"]["target"]["attempts"], 10)
        self.assertEqual(r["after"]["target"]["attempts"], 10)

    def test_adoption_share(self):
        ad = measure.adoption(fx.inline_ttt(), lambda e: (e.inline_target or "").startswith("ttt"),
                              lambda e: e.shape == "ttt", at=fx.T0 + timedelta(days=20))
        self.assertEqual(ad["new_since"], 15)
        self.assertEqual(ad["old_since"], 20)


class Governance(unittest.TestCase):
    """The learner may propose. Only a named human applies, and never beyond the evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ws = Workspace(os.path.join(self.root, ".runtune"))
        got, held = constraints.derive(fx.failing_psql())
        caps, _ = capabilities.derive_inline(fx.inline_ttt())
        rts, _ = routes.derive(fx.routing(), today=datetime(2026, 8, 30).date())
        self.ws.save_run({"run_id": "r1", "candidates": [c.to_dict() for c in got + caps + rts],
                          "withheld": [c.to_dict() for c in held]})
        self.psql = [c for c in got if "psql" in c.key][0].id
        self.cap = caps[0].id
        self.challenger = [c for c in rts if c.key.startswith("challenger")][0].id
        self.withheld = [c for c in held if "make" in c.key][0].id

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_apply_without_approver(self):
        promoter.stage(self.ws, self.psql, self.root)
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.psql, approver="")

    def test_ceiling_enforced(self):
        promoter.stage(self.ws, self.psql, self.root)
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.psql, "kai", action="block")
        art = promoter.apply(self.ws, self.psql, "kai", action="deny")
        rules = json.load(open(art["target"]))["rules"]
        self.assertEqual(rules[0]["action"], "deny")

    def test_withheld_needs_override_and_reason(self):
        with self.assertRaises(promoter.Refused):
            promoter.stage(self.ws, self.withheld, self.root)
        with self.assertRaises(promoter.Refused):
            promoter.stage(self.ws, self.withheld, self.root, allow_withheld=True)

    def test_stale_target_refused(self):
        promoter.stage(self.ws, self.psql, self.root)
        target = self.ws.get(self.psql)["target"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            json.dump({"rules": [{"id": "someone-else"}]}, f)
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.psql, "kai")

    def test_skill_never_overwrites_human_written(self):
        promoter.stage(self.ws, self.cap, self.root)
        target = self.ws.get(self.cap)["target"]
        os.makedirs(target)
        with open(os.path.join(target, "SKILL.md"), "w") as f:
            f.write("hand-written")
        # Even a proposal staged against this exact directory must not overwrite it:
        # ownership is checked separately from staleness.
        from runtune.lifecycle.store import file_digest
        art = self.ws.get(self.cap)
        art["base_digest"] = file_digest(target)
        self.ws.put(art)
        with self.assertRaises(promoter.Refused) as cm:
            promoter.apply(self.ws, self.cap, "kai")
        self.assertIn("not created by RunTune", str(cm.exception))
        self.assertEqual(open(os.path.join(target, "SKILL.md")).read(), "hand-written")

    def test_stale_skill_dir_refused(self):
        promoter.stage(self.ws, self.cap, self.root)
        target = self.ws.get(self.cap)["target"]
        os.makedirs(target)
        open(os.path.join(target, "SKILL.md"), "w").write("appeared after staging")
        with self.assertRaises(promoter.Refused) as cm:
            promoter.apply(self.ws, self.cap, "kai")
        self.assertIn("changed since", str(cm.exception))

    def test_widening_route_needs_reason_and_eval(self):
        routes_path = os.path.join(self.root, "routes.json")
        with open(routes_path, "w") as f:
            json.dump({"modes": {"extract": {"model": "deepseek/v4-flash"}}}, f)
        promoter.stage(self.ws, self.challenger, self.root)
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.challenger, "kai")                       # no reason
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.challenger, "kai", reason="cheaper")     # no eval
        ev = os.path.join(self.root, "eval.json")
        open(ev, "w").write("{}")
        promoter.apply(self.ws, self.challenger, "kai", reason="cheaper", eval_ref=ev)
        self.assertEqual(json.load(open(routes_path))["modes"]["extract"]["model"], "qwen/cheap")
        events = ledger.read(self.ws.ledger_path)
        self.assertTrue(any(e.get("boundary_change") for e in events if e["action"] == "apply"))

    def test_protected_paths(self):
        auth = self.ws.authority()
        auth["targets"]["constraint"] = ".claude/settings.json"
        json.dump(auth, open(os.path.join(self.ws.root, "authority.json"), "w"))
        promoter.stage(self.ws, self.psql, self.root)
        with self.assertRaises(promoter.Refused):
            promoter.apply(self.ws, self.psql, "kai")

    def test_revision_can_only_narrow_a_grant(self):
        path = os.path.join(self.root, "agent.md")
        open(path, "w").write("---\nname: a\ntools: Bash, Read\n---\nbody\n")
        with self.assertRaises(promoter.Refused):
            promoter._revise_agent_grant(path, ["Bash", "Read", "WebFetch"], "x")
        promoter._revise_agent_grant(path, ["Bash"], "x")
        self.assertIn("tools: Bash\n", open(path).read())

    def test_retire_constraint_is_a_boundary_change(self):
        promoter.stage(self.ws, self.psql, self.root)
        promoter.apply(self.ws, self.psql, "kai")
        with self.assertRaises(promoter.Refused):
            promoter.retire(self.ws, self.psql, "kai", reason="")
        promoter.retire(self.ws, self.psql, "kai", reason="workflow fixed")
        last = ledger.read(self.ws.ledger_path)[-1]
        self.assertTrue(last["boundary_change"])
        self.assertEqual(ledger.verify(self.ws.ledger_path)[0], True)

    def test_revision_inherits_cases(self):
        promoter.stage(self.ws, self.psql, self.root)
        promoter.apply(self.ws, self.psql, "kai")
        run = self.ws.latest_run()
        c2 = dict(run["candidates"][0])
        c2["id"] = "con-psql-v2"
        c2["proposal"] = {"ruleset_rule": {**c2["proposal"]["ruleset_rule"], "id": "v2",
                                           "any": [r"(?:^|\s)psql\s+-c"]}}
        run["candidates"].append(c2)
        run["run_id"] = "r2"
        self.ws.save_run(run)
        art = promoter.stage(self.ws, "con-psql-v2", self.root, revises=self.psql)
        self.assertEqual(len(art["cases"]), 2)
        self.assertEqual(art["lineage"], [self.psql])


class LedgerAndRedaction(unittest.TestCase):
    def test_tamper_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "l.jsonl")
            ledger.append(p, {"action": "a"})
            ledger.append(p, {"action": "b"})
            lines = open(p).read().splitlines()
            lines[0] = lines[0].replace('"a"', '"z"')
            open(p, "w").write("\n".join(lines) + "\n")
            self.assertFalse(ledger.verify(p)[0])

    def test_redacts_dsns_and_tokens(self):
        s = redact.redact("psql postgresql://u:p@host/db glpat-abcdefghijklmnopqrst OPENROUTER_API_KEY=sk-or-v1-abcdef123456789012")
        self.assertNotIn("u:p@host", s)
        self.assertNotIn("abcdefghijklmnop", s)
        self.assertNotIn("abcdef1234567890", s)


if __name__ == "__main__":
    unittest.main()
