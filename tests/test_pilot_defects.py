"""Regression tests for the four defects the first team pilot found (2026-10-13).

Each is tested over its class, not the one instance: every warehouse source, every
compound-command form, every route status. The instance that was found is named in
each test so the evidence doc can point here.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from runtune.derive import constraints, routes
from runtune.evidence import shapes
from runtune.evidence.schema import Corpus
from runtune.lifecycle import promoter
from runtune.lifecycle.store import Workspace
from runtune.notify import digest
from tests import fixtures as fx


class _FakePg:
    """Stand-in for pg.query: answers by which table the SQL names."""

    def __init__(self, answers):
        self.answers, self.seen = answers, []

    def __call__(self, dsn, sql, params=None):
        self.seen.append(sql)
        for needle, answer in self.answers.items():
            if needle in sql:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return []


def _with_pg(fake, fn):
    from runtune.sources import pg
    real = pg.query
    pg.query = fake
    try:
        return fn()
    finally:
        pg.query = real


class WarehouseGaps(unittest.TestCase):
    """Defect 1: an older cc-logger had no agent_invocations.token_source and runtune
    crashed with a traceback instead of reporting the gap."""

    SOURCES = {"claude": "tool_calls", "codex": "codex_tool_calls", "cursor": "cursor_tool_calls",
               "antigravity": "antigravity_tool_calls", "openrouter": "openrouter_calls"}

    def test_every_source_turns_a_missing_table_into_a_coverage_note(self):
        from runtune import sources
        for src, table in self.SOURCES.items():
            with self.subTest(source=src):
                fake = _FakePg({"": RuntimeError(f'relation "{table}" does not exist')})
                c = _with_pg(fake, lambda: sources.load([src], "postgresql://x", 30))
                self.assertEqual(c.events, [])
                self.assertTrue(c.coverage[src].notes, f"{src} was skipped without saying so")

    def test_every_source_turns_a_missing_column_into_a_coverage_note(self):
        from runtune import sources
        for src in self.SOURCES:
            with self.subTest(source=src):
                fake = _FakePg({"": RuntimeError("column c.exit_code does not exist")})
                c = _with_pg(fake, lambda: sources.load([src], "postgresql://x", 30))
                self.assertIn("c.exit_code", " ".join(c.coverage[src].notes))

    def test_other_errors_still_raise(self):
        from runtune import sources
        fake = _FakePg({"": RuntimeError("password authentication failed")})
        with self.assertRaises(RuntimeError):
            _with_pg(fake, lambda: sources.load(["claude"], "postgresql://x", 30))

    def _tool_row(self):
        return [{"tool_call_id": "t1", "session_id": "s1", "invocation_id": "i1", "tool_name": "Bash",
                 "actor": "root", "status": "success", "text": "git status", "error": "", "duration_ms": 5,
                 "ts": fx.T0, "model": "claude-opus-5"}]

    def test_claude_without_token_source_keeps_its_evidence_and_says_why_tokens_are_missing(self):
        from runtune.sources import claude
        inv = [{"invocation_id": "i1", "session_id": "s1", "agent_type": "general-purpose", "model": "m",
                "status": "completed", "started_at": fx.T0, "ended_at": fx.T0, "output_tokens": 9_000_000,
                "cache_read_tokens": 1_100_000_000, "token_source": None}]
        fake = _FakePg({"pg_attribute": [{"attname": a} for a in ("invocation_id", "session_id", "agent_type")],
                        "FROM tool_calls": self._tool_row(), "FROM agent_invocations": inv})
        c = _with_pg(fake, lambda: claude.load("postgresql://x", 30))
        self.assertEqual(len(c.events), 1)
        self.assertEqual(len(c.invocations), 1)
        self.assertEqual(c.invocations[0].tokens_reread, 0)          # unverified counts are not used
        self.assertIn("NULL::text AS token_source", [s for s in fake.seen if "agent_invocations" in s][-1])
        self.assertIn("no token_source column", " ".join(c.coverage["claude"].notes))

    def test_claude_without_an_invocations_table(self):
        from runtune.sources import claude
        fake = _FakePg({"pg_attribute": [], "FROM tool_calls": self._tool_row()})
        c = _with_pg(fake, lambda: claude.load("postgresql://x", 30))
        self.assertEqual((len(c.events), len(c.invocations)), (1, 0))
        self.assertIn("no agent_invocations table", " ".join(c.coverage["claude"].notes))

    def test_a_skipped_openrouter_source_still_applies_the_allowlist(self):
        from runtune import sources
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"modes": {"m": {"model": "x/y", "verified_date": "2026-01-01"}}}, f)
        fake = _FakePg({"": RuntimeError('relation "openrouter_calls" does not exist')})
        c = _with_pg(fake, lambda: sources.load(["openrouter"], "postgresql://x", 30, routes=f.name))
        os.unlink(f.name)
        self.assertIn("m", c.route_policy["modes"])


class StatusAttribution(unittest.TestCase):
    """Defect 2: a compound command's failure was blamed on its FIRST command, so
    `bison-report.py … | head; echo ===; …` produced a rule against a report that had
    worked, with its normal output filed as the error."""

    CASES = [
        # (command, the part whose exit status the call reports; None = cannot be told)
        ("python3 scripts/bison-report.py campaigns x 2>&1 | head -60; echo ===; "
         "python3 scripts/bison-report.py overview x 2>&1 | head -40", "head -40"),
        ("cd /a && python3 scripts/gl.py cat k a.md; echo; python3 scripts/gl.py cat k b.md",
         "python3 scripts/gl.py cat k b.md"),
        ('cd "/a b" && python3 x.py --y', "python3 x.py --y"),
        ("export A=1; set -e; python3 x.py", "python3 x.py"),
        ("python3 a.py && python3 b.py", None),
        ("python3 a.py || echo failed", None),
        ("for s in a b; do python3 gl.py ls $s; done", None),
        ("( cd x; make )", None),
        ("curl -s url > /tmp/x 2>&1 &", None),
        ("ls &> /dev/null", "ls &> /dev/null"),
        ("git log --oneline | head -3  # a comment ; not a split", "head -3"),
        ('python3 -c "import a; a.b() | 2 && 3"', 'python3 -c "import a; a.b() | 2 && 3"'),
        ("X=$(grep a b | head -1; echo z); echo $X", "echo $X"),
        ("echo 'a; b' && true", None),
    ]

    def test_status_part_of_each_form(self):
        for cmd, want in self.CASES:
            with self.subTest(cmd=cmd):
                got = shapes.status_command(cmd)
                self.assertEqual(got.split("  #")[0].strip() if got else got, want)

    def test_heredoc_bodies_are_never_split(self):
        cmd = "python3 - <<'EOF'\nimport os; print(1)\nif x && y: pass\nEOF"
        self.assertTrue(shapes.status_command(cmd).startswith("python3 - <<'EOF'"))
        self.assertEqual(shapes.status_command(cmd + "\necho done"), "echo done")

    def test_truncated_recording_with_several_parts_is_not_attributed(self):
        self.assertIsNone(shapes.status_command("python3 a.py; python3 b.py", truncated=True))
        self.assertEqual(shapes.status_command("python3 a.py --long", truncated=True), "python3 a.py --long")

    def _corpus(self):
        c = Corpus()
        chained = ("python3 scripts/report.py a 2>&1 | head -5; echo ===; python3 scripts/broken.py")
        for i in range(6):
            c.events.append(fx.ev(session=f"s{i % 3}", day=i, text=chained, ok=False,
                                  error="Exit code 1\nname status emails_sent"))
        for i in range(10):
            c.events.append(fx.ev(session=f"s{i % 4}", day=i, text="python3 scripts/report.py b"))
        for i in range(4):
            c.events.append(fx.ev(session=f"s{i % 2}", day=i, text="python3 scripts/a.py && python3 scripts/b.py",
                                  ok=False, error="Exit code 2\nboom"))
        return c.finalize()

    def test_constraints_key_on_the_part_that_failed(self):
        got, held = constraints.derive(self._corpus())
        keys = {c.key for c in got + held}
        self.assertNotIn("shell|python3 report.py", keys)
        self.assertIn("shell|python3 broken.py", keys)
        u = [c for c in held if c.key == "shell|(unattributed)"][0]
        self.assertEqual(u.numbers["fails"], 4)


class EnvironmentCause(unittest.TestCase):
    """Defect 3: one broken toolchain (`xcrun: unable to load libxcrun`) became four
    separate per-command rules, none of which would fix it."""

    XCRUN = "Exit code 1\nxcrun: error: unable to load libxcrun (dlopen(/Library/x, 0x0005): tried)"

    def _corpus(self, error, scripts=("a.py", "b.py", "c.py")):
        c = Corpus()
        for i, s in enumerate(scripts * 2):
            c.events.append(fx.ev(session=f"s{i % 2}", day=i, text=f"python3 scripts/{s} --x", ok=False, error=error))
        for i in range(12):
            c.events.append(fx.ev(session=f"s{i % 3}", day=i, text=f"python3 scripts/{scripts[i % len(scripts)]} --x"))
        return c.finalize()

    def test_one_cause_across_commands_is_one_environment_finding(self):
        got, held = constraints.derive(self._corpus(self.XCRUN))
        env = [c for c in got if c.kind == "environment"]
        self.assertEqual(len(env), 1)
        self.assertEqual(len(env[0].numbers["commands"]), 3)
        self.assertEqual(env[0].proposal, {})
        self.assertFalse([c for c in got + held if c.kind == "constraint" and "a.py" in c.key])

    def test_generic_and_warning_signatures_are_not_environment_causes(self):
        for err in ("Exit code 1\nTraceback (most recent call last):\n  File \"x\", line 3, in <module>",
                    "Exit code 1\n/usr/lib/x: NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+"):
            with self.subTest(err=err[:30]):
                got, held = constraints.derive(self._corpus(err))
                self.assertFalse([c for c in got + held if c.kind == "environment"])

    def test_shell_location_prefixes_do_not_split_one_cause(self):
        sig = constraints.environment_signature
        self.assertEqual(sig("exit code # /p: xcrun: error: unable to load libxcrun (dlopen"),
                         sig("exit code # xcrun: error: unable to load libxcrun (dlopen"))
        self.assertEqual(sig("exit code # (eval):#: command not found: timeout"), "command not found: timeout")
        self.assertEqual(sig("exit code # /P /P xcrun: error: unable to load libxcrun (dlopen"),
                         sig("exit code # xcrun: error: unable to load libxcrun (dlopen"))

    def test_normal_output_recorded_as_an_error_is_not_an_environment_cause(self):
        got, held = constraints.derive(self._corpus("Exit code 1\n# How to draft a comment > **This file is the framework.**"))
        self.assertFalse([c for c in got + held if c.kind == "environment"])

    def test_a_harness_timeout_is_not_an_environment_cause(self):
        got, held = constraints.derive(self._corpus("Exit code 124\nCommand timed out after 2m 0s"))
        self.assertFalse([c for c in got + held if c.kind == "environment"])

    def test_two_commands_is_not_enough(self):
        got, held = constraints.derive(self._corpus(self.XCRUN, scripts=("a.py", "b.py")))
        self.assertFalse([c for c in got + held if c.kind == "environment"])

    def test_an_environment_finding_cannot_be_staged_and_is_acknowledged_by_reply(self):
        got, _ = constraints.derive(self._corpus(self.XCRUN))
        env = [c for c in got if c.kind == "environment"][0]
        with tempfile.TemporaryDirectory() as root:
            ws = Workspace(os.path.join(root, ".runtune"))
            ws.save_run({"run_id": "r1", "candidates": [env.to_dict()], "withheld": []})
            with self.assertRaises(promoter.Refused):
                promoter.stage(ws, env.id, root)
            self.assertIn("a fix on the machine", digest._meta(env.to_dict()))


class RoutesJudgedAtCallTime(unittest.TestCase):
    """Defect 4: calls made while a since-retired mode was in force were reported as
    unapproved, and the `routes` table called pre-clearance and benchmark traffic drift."""

    def _corpus(self):
        c = fx.routing()
        c.route_policy["retired"] = {"old-rung": {"retired_date": "2026-08-10", "reason": "x"}}
        for i in range(25):        # before retirement -> history
            c.events.append(fx.ev("openrouter", "job", day=i % 5, surface="old-rung", kind="model", model="o/m"))
        for i in range(25):        # after retirement -> unapproved
            c.events.append(fx.ev("openrouter", "job", day=12 + i % 5, surface="old-rung", kind="model",
                                  model="o/m"))
        return c.finalize()

    def test_retired_mode_is_judged_by_its_retirement_date(self):
        got, held = routes.derive(self._corpus(), today=datetime(2026, 8, 30).date())
        self.assertIn("preretirement|old-rung|o/m", {c.key for c in held})
        un = [c for c in got if c.key == "unapproved|old-rung|o/m"][0]
        self.assertEqual(un.numbers["calls"], 25)

    def test_table_and_deriver_agree_on_every_call(self):
        corpus = self._corpus()
        got, held = routes.derive(corpus, today=datetime(2026, 8, 30).date())
        table = routes.traffic_table(corpus)
        statuses = {r["status"] for r in table}
        self.assertTrue({"bench", "pre-clearance", "pre-retirement", "drift", "approved"} <= statuses)
        by_key = {c.key: c for c in got}
        for r in table:
            if r["status"] in ("drift", "unapproved") and r["calls"] >= routes.MIN_CALLS:
                key = f"{'drift' if r['status'] == 'drift' else 'unapproved'}|{r['mode']}|{r['model']}"
                self.assertEqual(by_key[key].numbers["calls"], r["calls"], key)


class RoutesSchemaContract(unittest.TestCase):
    """Connection 2: RunTune reaches model-eval-gate's verdict on every case in its shared
    case file (test/routes_cases.json), and reports a failing file as a finding."""

    @staticmethod
    def _meg():
        repo = os.environ.get("MEG_REPO", os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "model-eval-gate"))
        cases = os.path.join(repo, "test", "routes_cases.json")
        return repo, cases

    def test_shared_cases_reach_the_same_verdict(self):
        import sys
        from runtune.sources.openrouter import policy_errors
        repo, cases = self._meg()
        if not os.path.exists(cases):
            self.skipTest("model-eval-gate checkout not found (set MEG_REPO)")
        sys.path.insert(0, repo)
        try:
            for c in json.load(open(cases)):
                with self.subTest(case=c["name"]):
                    errs, validator = policy_errors(c["routes"])
                    self.assertEqual(validator, "model-eval-gate")
                    self.assertEqual(not errs, c["valid"], errs)
                    if c.get("error_contains") and c["routes"].get("modes") is not None:
                        self.assertTrue(any(c["error_contains"] in e for e in errs), errs)
        finally:
            sys.path.remove(repo)

    def test_without_model_eval_gate_the_fields_runtune_reads_are_checked(self):
        import builtins
        from runtune.sources.openrouter import policy_errors
        real = builtins.__import__
        def no_meg(name, *a, **k):
            if name.startswith("meg"):
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = no_meg
        try:
            errs, validator = policy_errors({"modes": {"m": {"model": "x/y", "verified_date": "Oct 1"}},
                                             "retired": {"r": {"reason": "z", "retired_date": "soon"}}})
        finally:
            builtins.__import__ = real
        self.assertTrue(validator.startswith("runtune"))
        self.assertEqual(len(errs), 2)

    def test_a_failing_file_is_a_finding_that_needs_a_person(self):
        c = fx.routing()
        c.route_policy["errors"] = ["modes.dial.evidence_ref: required non-empty string"]
        got, _ = routes.derive(c, today=datetime(2026, 8, 30).date())
        s = [x for x in got if x.key == "schema|routes.json"][0]
        self.assertTrue(digest.needs_judgment(s.to_dict()))


class RegressionFeedsRunTune(unittest.TestCase):
    """Connection 3: model-eval-gate's regression results become route findings, a clean
    run names itself as the re-check a stale clearance needs, and review re-validates an
    applied route change when its mode later drifts."""

    def _write(self, results, ran_at="2026-08-28T06:00:00Z"):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"ran_at": ran_at, "results": results}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _derive(self, results, **kw):
        from runtune.sources import regression
        c = fx.routing()
        c.regression = regression.load(self._write(results, **kw))
        return routes.derive(c, today=datetime(2026, 8, 30).date())

    def test_both_result_formats_load(self):
        from runtune.sources import regression
        new = regression.load(self._write([{"mode": "m", "model": "x/y", "signals": [
            {"type": "model_quality_drift", "detail": "fell"}], "inconclusive": False}]))
        old = regression.load(self._write([{"mode": "m", "model": "x/y", "drift": ["meanScore fell"]}]))
        self.assertEqual(new["modes"]["m"]["model_drift"][0]["type"], "model_quality_drift")
        self.assertEqual(old["modes"]["m"]["model_drift"][0]["type"], "drift")
        clean = regression.load(self._write([{"mode": "m", "model": "x/y", "drift": []}]))
        self.assertTrue(clean["modes"]["m"]["clean"])

    def test_model_drift_on_the_routed_model_is_a_finding(self):
        got, _ = self._derive([{"mode": "dial", "model": "google/flash-lite", "signals": [
            {"type": "consistency", "detail": "pass^k 0.40 < floor 0.5"}]}])
        c = [x for x in got if x.key == "regression|dial"][0]
        self.assertIn("eval", c.requires)

    def test_what_is_not_model_drift_is_withheld_with_its_reason(self):
        cases = {
            "inconclusive": [{"mode": "dial", "model": "google/flash-lite", "inconclusive": True,
                              "signals": [{"type": "provider_unavailable", "detail": "9/9"}]}],
            "infra only": [{"mode": "dial", "model": "google/flash-lite",
                            "signals": [{"type": "parse_failure", "detail": "3/9"}]}],
            "another model": [{"mode": "dial", "model": "openai/old", "signals": [
                {"type": "model_quality_drift", "detail": "fell"}]}],
            "not a mode now": [{"mode": "gone", "model": "a/b", "signals": [
                {"type": "model_quality_drift", "detail": "fell"}]}],
        }
        for name, results in cases.items():
            with self.subTest(case=name):
                got, held = self._derive(results)
                self.assertFalse([x for x in got if x.key.startswith("regression|")])
                self.assertTrue([x for x in held if x.key.startswith("regression|")])

    def test_a_clean_run_after_the_clearance_names_the_recheck(self):
        got, _ = self._derive([{"mode": "idle-mode", "model": "qwen/qwen3", "drift": []}])
        life = [x for x in got if x.key == "lifecycle|idle-mode"][0]
        # idle and stale: the clean run is stated, but the ask stays "retire it" — nobody uses it
        self.assertEqual(life.numbers["regression_clean_on"], "2026-08-28")
        self.assertTrue(life.claim.startswith("a clearance nothing uses"))
        c = fx.routing()
        from runtune.sources import regression
        c.route_policy["modes"]["extract"]["verified_date"] = "2026-03-01"
        c.regression = regression.load(self._write([{"mode": "extract", "model": "deepseek/v4-flash", "drift": []}]))
        got, _ = routes.derive(c, today=datetime(2026, 8, 30).date())
        life = [x for x in got if x.key == "lifecycle|extract"][0]
        self.assertEqual(life.numbers["regression_clean_on"], "2026-08-28")
        self.assertIn("re-clear", life.claim)

    def test_review_revalidates_an_applied_route_change_when_its_mode_drifts(self):
        from runtune.lifecycle import review
        from runtune.sources import regression
        c = fx.routing()
        c.regression = regression.load(self._write([{"mode": "dial", "model": "google/flash-lite", "signals": [
            {"type": "model_quality_drift", "detail": "fell"}]}]))
        art = {"id": "rou-x", "kind": "route", "cases": [{"type": "route", "key": "lifecycle|dial"}]}
        verdict, _ = review._judge(art, c, datetime(2026, 8, 1, tzinfo=timezone.utc), 28)
        self.assertEqual(verdict, "revalidate")
        later, _ = review._judge(art, c, datetime(2026, 9, 1, tzinfo=timezone.utc), 28)
        self.assertNotEqual(later, "revalidate")       # the drift predates this change


class DelegationThroughModelEvalGate(unittest.TestCase):
    """Connection 4: model-eval-gate's workload classes, run on RunTune's evidence."""

    def setUp(self):
        import sys
        repo = os.environ.get("MEG_REPO", os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "model-eval-gate"))
        if not os.path.exists(os.path.join(repo, "meg")):
            self.skipTest("model-eval-gate checkout not found (set MEG_REPO)")
        sys.path.insert(0, repo)
        self.addCleanup(sys.path.remove, repo)

    def _corpus(self):
        c = Corpus()
        for i in range(40):
            c.events.append(fx.ev(session=f"s{i % 4}", day=i % 10, text="python3 scripts/digest.py --batch"))
        for i in range(60):
            c.events.append(fx.ev(session=f"s{i % 4}", day=i % 10, text="git status"))
        return c.finalize()

    def test_api_billed_work_asks_the_stakes_question(self):
        from runtune.derive import delegation
        got, held = delegation.derive(self._corpus(), host_billing="api")
        keys = {c.key for c in got}
        self.assertIn("delegation|digest.py", keys)
        self.assertFalse([k for k in keys | {c.key for c in held} if "git" in k])   # a habit, not a task
        c = [x for x in got if x.key == "delegation|digest.py"][0]
        self.assertEqual(c.numbers["runs"], 40)
        self.assertIn("eval", c.requires)

    def test_subscription_work_is_one_honest_line_not_a_saving(self):
        from runtune.derive import delegation
        got, held = delegation.derive(self._corpus(), host_billing="subscription")
        self.assertEqual(got, [])
        f = [x for x in held if x.key == "delegation|subscription"][0]
        self.assertIn("saves $0", f.title)

    def test_without_model_eval_gate_it_says_it_did_not_run(self):
        import builtins
        from runtune.derive import delegation
        real = builtins.__import__
        def no_meg(name, *a, **k):
            if name.startswith("meg"):
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = no_meg
        try:
            got, held = delegation.derive(self._corpus(), host_billing="api")
        finally:
            builtins.__import__ = real
        self.assertEqual(got, [])
        self.assertEqual(held[0].key, "delegation|unavailable")


if __name__ == "__main__":
    unittest.main()
