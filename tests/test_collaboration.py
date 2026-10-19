"""Sub-agent provenance, workflow runs and cross-session messages (2026-10-13).

cc-logger started recording what each sub-agent was asked to do, every Workflow run, and
both ends of every cross-session message. These tests cover each path the data takes
into RunTune: the warehouse reader (with and without the newer tables), the transcript
recorder, the JSONL loader that joins a message's two ends, the sub-agent deriver that
now names fan-outs by task, the coordination deriver, and the hand-off to
model-eval-gate.
"""

import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from runtune.derive import coordination, subagents
from runtune.evidence.schema import Corpus, Invocation, Message, WorkflowRun, merge_messages
from runtune.lifecycle import promoter
from runtune.lifecycle.store import Workspace
from tests import fixtures as fx

T0 = fx.T0


def _inv(n, agent_type="workflow-subagent", run=None, wf=None, phase=None, desc=None, session="s1",
         tools=None, day=0, prompt=None):
    return Invocation(source="claude", invocation=f"a{n}", session=session, agent_type=agent_type,
                      started=T0 + timedelta(days=day, minutes=n), ended=T0 + timedelta(days=day, minutes=n + 1),
                      status="completed", tools=Counter(tools or {"Bash": 3}), description=desc,
                      workflow_run=run, workflow_name=wf, workflow_phase=phase, prompt=prompt)


def _msg(n, at, sender, receiver, first="Deployed abc1234 to web-app", body=None, idle=False):
    return Message(source="claude", msg_id=f"m{n}", at=at, sender=sender, receiver=receiver,
                   first_line=first, body=body if body is not None else first, body_chars=len(body or first),
                   notify_when_idle=idle)


class TaskNames(unittest.TestCase):
    def test_a_workflow_agent_is_named_by_workflow_and_phase(self):
        self.assertEqual(_inv(1, wf="market-fanout", phase="Verify", desc="verify:ITEM1").task, "market-fanout/verify")

    def test_without_a_phase_the_label_prefix_names_the_step(self):
        self.assertEqual(_inv(1, wf="market-fanout", desc="research:ITEM2").task, "market-fanout/research")

    def test_an_agent_call_is_named_by_its_description(self):
        self.assertEqual(_inv(1, agent_type="general-purpose", desc="verify:Example ISD").task, "verify")
        self.assertEqual(_inv(1, agent_type="Explore", desc="Find the hook config").task, "find the hook config")
        self.assertIsNone(_inv(1, agent_type="Explore").task)


class _Pg:
    """pg.query stand-in that knows which tables and columns exist."""

    def __init__(self, tables, answers):
        self.tables, self.answers = tables, answers

    def __call__(self, dsn, sql, params=None):
        if "pg_attribute" in sql:
            return [{"attname": c} for c in self.tables.get(params[0], ())]
        for needle, rows in self.answers.items():
            if needle in sql:
                return rows
        return []


def _load(fake):
    from runtune.sources import claude, pg
    real_q = pg.query
    pg.query = fake
    try:
        return claude.load("postgres://x", 30)
    finally:
        pg.query = real_q


OLD_INV = {"invocation_id", "session_id", "agent_type", "model", "status", "started_at", "ended_at",
           "output_tokens", "cache_read_tokens", "token_source"}
NEW_INV = OLD_INV | {"description", "prompt_received", "spawned_by_tool_call_id", "workflow_run_id",
                     "workflow_phase", "last_message", "source"}


class WarehouseReader(unittest.TestCase):
    def _answers(self):
        return {
            "FROM tool_calls": [],
            "FROM agent_invocations": [{"invocation_id": "a1", "session_id": "s1", "agent_type": "web-researcher",
                                        "status": "completed", "started_at": T0, "ended_at": T0,
                                        "token_source": None, "description": "verify:ITEM1",
                                        "prompt": "Verify the stat. key sk-abcdefghijklmnopqrstuvwxyz123456",
                                        "parent_call": None, "workflow_run_id": "wf_1", "result_chars": 900,
                                        "workflow_phase": "Research",
                                        "row_source": "backfill"}],
            "FROM workflow_runs": [{"run_id": "wf_1", "session_id": "s1", "workflow_name": "market-fanout",
                                    "status": "completed", "started_at": T0, "duration_ms": 1000,
                                    "agent_count": 10, "total_tokens": 5000}],
            "FROM vw_peer_messages": [{"msg_id": "m1", "at": T0, "sender_session_id": "s1",
                                       "sender_inferred": False, "sender_name": "session-a",
                                       "receiver_session_id": "s2", "addressed_to": "session-b",
                                       "delivery": "mid_turn", "notify_when_idle": False,
                                       "body": "\nShipped 1a2b3c4 on main\nNo reply needed.", "body_chars": 40}],
        }

    def test_newer_logger_gives_provenance_runs_and_messages(self):
        tables = {"agent_invocations": NEW_INV, "workflow_runs": {"run_id"}, "vw_peer_messages": {"msg_id"}}
        c = _load(_Pg(tables, self._answers()))
        inv = c.invocations[0]
        self.assertEqual(inv.workflow_name, "market-fanout")
        self.assertEqual(inv.task, "market-fanout/research")            # the recorded phase beats the label
        self.assertTrue(inv.recovered)
        self.assertNotIn("sk-abcdefghij", inv.prompt)                     # redacted on the way in
        self.assertEqual(c.messages[0].first_line, "Shipped 1a2b3c4 on main")
        self.assertTrue(c.messages[0].matched)
        self.assertTrue({"Workflow", "SendMessage", "ListAgents"} <= c.capture_allowlist["claude"])

    def test_older_logger_is_a_coverage_note_not_an_error(self):
        tables = {"agent_invocations": OLD_INV}
        c = _load(_Pg(tables, self._answers()))
        notes = " ".join(c.coverage["claude"].notes)
        self.assertIn("no sub-agent provenance", notes)
        self.assertIn("no workflow_runs table", notes)
        self.assertIn("no vw_peer_messages view", notes)
        self.assertEqual(c.messages, [])
        # an older logger never captured these tools: they must not read as "unused"
        self.assertNotIn("SendMessage", c.capture_allowlist["claude"])


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TranscriptRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "projects", "-repo")
        os.environ["RUNTUNE_HOME"] = os.path.join(self.tmp, "home")
        self.addCleanup(os.environ.pop, "RUNTUNE_HOME", None)

    def _sender(self):
        return [
            {"type": "assistant", "sessionId": "S", "timestamp": "2026-10-13T10:00:00Z",
             "message": {"model": "m", "content": [
                 {"type": "tool_use", "id": "t1", "name": "SendMessage",
                  "input": {"to": "session-b", "message": "Deployed 9f8e7d6 to chat-agent\nNo reply needed."}},
                 {"type": "tool_use", "id": "t2", "name": "SendMessage",
                  "input": {"to": "subagent-x", "message": "keep going"}}]}},
            {"type": "user", "sessionId": "S", "timestamp": "2026-10-13T10:00:01Z",
             "toolUseResult": {"msg_id": "M1", "message": "Delivered"},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
            {"type": "user", "sessionId": "S", "timestamp": "2026-10-13T10:00:01Z",
             "toolUseResult": {"message": "sent to subagent"},           # no msg_id: not cross-session
             "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        ]

    def _receiver(self):
        return [
            {"type": "attachment", "sessionId": "R", "timestamp": "2026-10-13T10:00:02Z",
             "attachment": {"type": "queued_command", "origin": {
                 "kind": "peer", "msg_id": "M1", "from": "uds:/tmp/cc-socks/1.sock", "name": "session-c",
                 "body": "Deployed 9f8e7d6 to chat-agent\nNo reply needed."}}},
            {"type": "user", "isMeta": True, "sessionId": "R", "timestamp": "2026-10-13T10:05:00Z",
             "origin": {"kind": "peer", "handback": True, "msg_id": "H1", "body": "sub-agent report"}},
        ]

    def test_both_ends_join_into_one_message_and_handbacks_are_excluded(self):
        from runtune.record import claude as rec
        from runtune.sources import local
        _write_jsonl(os.path.join(self.proj, "S.jsonl"), self._sender())
        _write_jsonl(os.path.join(self.proj, "R.jsonl"), self._receiver())
        rec.ingest(os.path.join(self.tmp, "projects"))
        c = local.load(days=36500)
        self.assertEqual([m.msg_id for m in c.messages], ["M1"])
        m = c.messages[0]
        self.assertEqual((m.sender, m.receiver, m.delivery), ("S", "R", "mid_turn"))
        self.assertEqual(m.first_line, "Deployed 9f8e7d6 to chat-agent")

    def test_a_subagent_carries_its_task_and_its_workflow_run(self):
        from runtune.record import claude as rec
        from runtune.sources import local
        sub = os.path.join(self.proj, "S", "subagents", "workflows", "wf_9", "agent-x1.jsonl")
        _write_jsonl(sub, [
            {"type": "user", "sessionId": "S", "agentId": "x1", "timestamp": "2026-10-13T10:00:00Z",
             "message": {"content": "[Workflow harness] Verify the ITEM1 stat"}},
            {"type": "assistant", "sessionId": "S", "agentId": "x1", "timestamp": "2026-10-13T10:00:05Z",
             "message": {"model": "haiku", "usage": {"output_tokens": 10, "cache_read_input_tokens": 100},
                         "content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "sessionId": "S", "agentId": "x1", "timestamp": "2026-10-13T10:00:06Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": "a"}]}},
        ])
        with open(sub[:-len(".jsonl")] + ".meta.json", "w") as f:
            json.dump({"agentType": "web-researcher", "description": "verify:ITEM1", "workflowPhase": "Verify"}, f)
        os.makedirs(os.path.join(self.proj, "S", "workflows"), exist_ok=True)
        with open(os.path.join(self.proj, "S", "workflows", "wf_9.json"), "w") as f:
            json.dump({"runId": "wf_9", "workflowName": "market-fanout", "status": "completed",
                       "startTime": 1791885600000, "agentCount": 2, "totalTokens": 900}, f)
        rec.ingest(os.path.join(self.tmp, "projects"))
        c = local.load(days=36500)
        inv = [i for i in c.invocations if i.invocation == "x1"][0]
        self.assertEqual(inv.workflow_run, "wf_9")
        self.assertEqual(inv.workflow_name, "market-fanout")              # joined from the run record
        self.assertEqual(inv.task, "market-fanout/verify")
        self.assertIn("Verify the ITEM1 stat", inv.prompt)
        self.assertEqual([(w.run_id, w.agents_declared) for w in c.workflow_runs], [("wf_9", 2)])

    def test_a_growing_subagent_transcript_loses_no_event(self):
        """The slice used to count the invocation record, so the next read skipped an event."""
        from runtune.record import claude as rec
        from runtune.sources import local
        sub = os.path.join(self.proj, "S", "subagents", "agent-y1.jsonl")
        calls = []

        def call(i):
            return [{"type": "assistant", "sessionId": "S", "agentId": "y1",
                     "timestamp": f"2026-10-13T10:{i:02d}:00Z",
                     "message": {"model": "m", "usage": {"output_tokens": 1},
                                 "content": [{"type": "tool_use", "id": f"c{i}", "name": "Bash",
                                              "input": {"command": f"echo {i}"}}]}},
                    {"type": "user", "sessionId": "S", "agentId": "y1", "timestamp": f"2026-10-13T10:{i:02d}:01Z",
                     "message": {"content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "x"}]}}]
        for i in range(3):
            calls += call(i)
        _write_jsonl(sub, calls)
        rec.ingest(os.path.join(self.tmp, "projects"))
        for i in range(3, 6):
            calls += call(i)
        _write_jsonl(sub, calls)
        os.utime(sub, (1, 2_000_000_000))
        rec.ingest(os.path.join(self.tmp, "projects"))
        c = local.load(days=36500)
        texts = sorted(e.text for e in c.events if e.session == "S")
        self.assertEqual(texts, [f"echo {i}" for i in range(6)])


class MessageJoin(unittest.TestCase):
    def test_non_empty_fields_win_and_earliest_time_wins(self):
        a = Message(source="claude", msg_id="m", at=T0 + timedelta(seconds=5), sender=None, receiver="R",
                    delivery="new_turn", first_line="x")
        b = Message(source="claude", msg_id="m", at=T0, sender="S", receiver=None, addressed_to="session-b")
        [m] = merge_messages([a, b])
        self.assertEqual((m.sender, m.receiver, m.addressed_to, m.delivery, m.at), ("S", "R", "session-b",
                                                                                     "new_turn", T0))


class SubagentsByTask(unittest.TestCase):
    def _corpus(self):
        c = Corpus()
        n = 0
        for run in range(4):
            for k in range(6):
                n += 1
                c.invocations.append(_inv(n, run=f"wf_{run}", wf="market-fanout", phase="Verify",
                                          desc=f"verify:item{k}", session=f"s{run}", day=run * 8,
                                          tools={"Bash": 2, "WebFetch": 1}))
        return c

    def test_workflow_fanouts_are_runs_and_the_finding_names_the_workflow(self):
        got, held = subagents.derive(self._corpus())
        c = (got + held)[0]
        self.assertEqual(c.numbers["fanouts"], 4)
        self.assertIn("`market-fanout`", c.title)
        self.assertEqual(c.numbers["tasks"], {"market-fanout/verify": 24})
        self.assertEqual(c.proposal["use_in"], ["market-fanout"])
        self.assertIn("name: market-fanout-verify-worker", c.proposal["agent_md"])
        self.assertFalse([x for x in c.caveats if "not by task" in x])

    def test_a_workflow_that_already_switched_reports_only_the_remainder(self):
        c = self._corpus()                                   # 24 generic agents, days 0-24
        for n in range(5):
            c.invocations.append(_inv(100 + n, agent_type="page-researcher", run="wf_new", wf="market-fanout",
                                      day=20, tools={"Bash": 1}))
        got, held = subagents.derive(c)
        f = [x for x in got + held if "market-fanout" in x.title][0]
        self.assertEqual(f.numbers["typed_as"], "page-researcher")
        self.assertEqual(f.numbers["generic_since_typed"], 6)            # run 3, day 24
        self.assertIn("still ran 6 agents", f.title)
        self.assertEqual(f.proposal["revises"], "page-researcher")

    def test_a_workflow_fully_switched_is_withheld_as_fixed(self):
        c = self._corpus()
        c.invocations.append(_inv(200, agent_type="page-researcher", run="wf_new", wf="market-fanout", day=30))
        got, held = subagents.derive(c)
        f = [x for x in held if "market-fanout" in x.title][0]
        self.assertIn("already fixed", [g for g in f.gates if g.name == "still-generic"][0].reason)

    def test_without_provenance_the_old_caveat_stays(self):
        c = Corpus()
        for i in range(12):
            c.invocations.append(_inv(i, session=f"s{i // 6}", day=(i // 6) * 8))
        got, held = subagents.derive(c)
        self.assertTrue([x for x in (got + held)[0].caveats if "not by task" in x])


class Coordination(unittest.TestCase):
    def _deploys(self, n_pairs, messaged=0, start_day=0):
        c = Corpus()
        for i in range(n_pairs):
            day = start_day + i * 3
            c.events.append(fx.ev(session=f"a{i}", day=day, minute=0, text="fly deploy -a chat-agent"))
            c.events.append(fx.ev(session=f"b{i}", day=day, minute=90, text="cd deploy/x && fly deploy -a chat-agent"))
            if i < messaged:
                c.messages.append(_msg(i, T0 + timedelta(days=day, minutes=30), f"b{i}", f"a{i}"))
        c.events.append(fx.ev(session="z", day=0, minute=1, surface="SendMessage", text="session-b"))
        return c.finalize()

    def test_deploy_targets(self):
        cases = {"fly deploy -a chat-agent": "chat-agent",
                 "cd deploy/social-fly && fly deploy": "social-fly",
                 "python3 scripts/deploy-tool.py deploy ops-app": "ops-app",
                 "fly deploy --config deploy/case-worker/fly.draft.toml": "case-worker",
                 "python3 scripts/deploy-tool.py deploy --stale": None,
                 "fly deploy --help": None, "fly status -a chat-agent": None}
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(coordination.deploy_target(text), want)

    def test_silent_handoffs_are_a_finding(self):
        got, held = coordination.derive(self._deploys(6, messaged=1))
        c = [x for x in got if x.key == "handoff|deploy"][0]
        self.assertEqual((c.numbers["since_messaging"], c.numbers["without_message"]), (6, 5))
        self.assertIn("chat-agent", c.claim)

    def test_one_week_of_messaging_is_a_baseline_not_a_pattern(self):
        c = self._deploys(6, messaged=0)
        late = Corpus()
        late.events = [e for e in c.events if e.ts >= T0 + timedelta(days=9)]
        late.messages.append(_msg(99, T0 + timedelta(days=9), "x", "y"))
        got, held = coordination.derive(late.finalize())
        h = [x for x in got + held if x.key == "handoff|deploy"][0]
        self.assertFalse(h.admitted)
        self.assertIn("messaging seen for", [g for g in h.gates if g.name == "breadth"][0].reason)

    def test_messaged_handoffs_are_not(self):
        got, held = coordination.derive(self._deploys(6, messaged=6))
        c = [x for x in held if x.key == "handoff|deploy"][0]
        self.assertEqual(c.numbers["without_message"], 0)

    def test_before_messaging_existed_silence_says_nothing(self):
        c = Corpus()
        for i in range(6):
            c.events.append(fx.ev(session=f"a{i}", day=i * 3, text="fly deploy -a app1"))
            c.events.append(fx.ev(session=f"b{i}", day=i * 3, minute=30, text="fly deploy -a app1"))
        got, held = coordination.derive(c.finalize())
        h = [x for x in held if x.key == "handoff|deploy"][0]
        self.assertEqual(h.numbers["since_messaging"], 0)
        self.assertFalse([g for g in h.gates if g.name == "messaging-in-use"][0].passed)

    def test_same_session_and_scratch_files_are_not_handoffs(self):
        c = Corpus()
        c.events += [fx.ev(session="a", surface="Edit", text="/tmp/x.py"),
                     fx.ev(session="b", surface="Edit", text="/tmp/x.py", minute=5),
                     fx.ev(session="a", surface="Edit", text="/repo/a.py"),
                     fx.ev(session="a", surface="Edit", text="/repo/a.py", minute=5)]
        self.assertEqual(coordination.handoffs(c.finalize()), [])

    def test_message_form_is_judged_per_rule(self):
        c = Corpus()
        for i in range(20):
            first = "Deployed 9f8e7d6 to web-app" if i < 15 else "hey, quick thing"
            c.messages.append(_msg(i, T0, f"s{i}", "r", first=first,
                                   body=first + ("\nFrom session session-a on feat/x. No reply needed." if i < 5 else "")))
        c = c.finalize()
        got, held = coordination.derive(c)
        f = [x for x in got if x.key == "message-form"][0]
        self.assertEqual(f.numbers["rates"]["checkable first line"], 0.75)
        self.assertEqual(f.numbers["rates"]["says who is sending"], 0.25)
        self.assertIn("says who is sending", f.title)

    def test_polling_without_a_notice(self):
        c = Corpus()
        for s in ("p1", "p2", "p3"):
            for m in range(3):
                c.events.append(fx.ev(session=s, surface="ListAgents", minute=m * 5))
        c.events += [fx.ev(session="ok", surface="ListAgents", minute=m * 5) for m in range(3)]
        c.messages.append(_msg(1, T0, "ok", "x", idle=True))
        got, held = coordination.derive(c.finalize())
        p = [x for x in got if x.key == "polling"][0]
        self.assertEqual(p.numbers["polling_sessions"], 3)

    def test_promoter_refuses_to_stage_a_coordination_finding(self):
        got, _ = coordination.derive(self._deploys(6))
        with tempfile.TemporaryDirectory() as root:
            ws = Workspace(os.path.join(root, ".runtune"))
            ws.save_run({"run_id": "r1", "candidates": [got[0].to_dict()], "withheld": []})
            with self.assertRaises(promoter.Refused) as e:
                promoter.stage(ws, got[0].id, root)
        self.assertIn("how sessions coordinate", str(e.exception))


class WorkflowCoverage(unittest.TestCase):
    def test_a_run_that_logged_fewer_agents_than_it_declared_is_named(self):
        from runtune.sources import workflow_summary
        c = Corpus()
        c.workflow_runs.append(WorkflowRun("claude", "wf_1", "s1", "fan", "completed", T0, agents_declared=10))
        c.invocations += [_inv(i, run="wf_1") for i in range(4)]
        s = workflow_summary(c)
        self.assertIn("10 agents declared, 4 logged", s)
        self.assertIn("1 run(s) logged fewer agents", s)

    def test_a_killed_run_that_logged_more_than_its_record_says_why(self):
        from runtune.sources import workflow_summary
        c = Corpus()
        c.workflow_runs.append(WorkflowRun("claude", "wf_k", "s1", "fan", "killed", T0, agents_declared=2))
        c.invocations += [_inv(i, run="wf_k") for i in range(5)]
        self.assertIn("all killed", workflow_summary(c))


class SubagentWorkReachesModelEvalGate(unittest.TestCase):
    def setUp(self):
        import sys
        repo = os.environ.get("MEG_REPO", os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "model-eval-gate"))
        if not os.path.exists(os.path.join(repo, "meg")):
            self.skipTest("model-eval-gate checkout not found (set MEG_REPO)")
        sys.path.insert(0, repo)
        self.addCleanup(sys.path.remove, repo)

    def test_a_repeated_subagent_task_is_a_delegation_class(self):
        from runtune.derive import delegation
        c = Corpus()
        for n in range(40):
            c.invocations.append(_inv(n, agent_type="web-researcher", run=f"wf_{n % 5}", wf="market-fanout",
                                      phase="Verify", session=f"s{n % 5}", day=n % 10))
        got, held = delegation.derive(c.finalize(), host_billing="api")
        keys = {x.key for x in got + held}
        self.assertIn("delegation|web-researcher [market-fanout/verify]", keys)


if __name__ == "__main__":
    unittest.main()
