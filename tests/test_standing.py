"""Standing: a skill or sub-agent is on trial until distinct sessions show it working.

These hold:
- an applied capability starts as `trial`; a grant narrowing has no standing;
- a session counts once, by how its last use ended (a failure fixed by a retry is clean);
- only sessions after the change went live count; a change waiting on its merge has no uses;
- three distinct clean sessions promote it (ledger only, no file changes);
- the last two using sessions ending badly make review propose a demotion;
- the digest offers the demotion first; approving it retires the artifact and restores the
  revision it replaced, byte for byte; declining it snoozes it like any other item.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from datetime import timedelta

from runtune.derive import capabilities
from runtune.evidence.schema import Corpus, Invocation
from runtune.lifecycle import ledger, promoter, review, standing
from runtune.lifecycle.store import Workspace
from runtune.notify import digest

from . import fixtures as fx

CLI = "scripts/toolkit db master 'select 1'"


def corpus(*sessions):
    """sessions: (name, day, [ok, ok, ...]) — toolkit CLI calls after T0."""
    c = Corpus()
    for name, day, oks in sessions:
        for m, ok in enumerate(oks):
            c.events.append(fx.ev("claude", name, day=day, text=CLI, ok=ok, error="" if ok else "toolkit: error: x",
                                  minute=m))
    return c.finalize()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ws = Workspace(os.path.join(self.root, ".runtune"))
        caps, _ = capabilities.derive_inline(fx.inline_ttt())
        self.cand = caps[0].to_dict()
        self.rev = copy.deepcopy(self.cand)
        self.rev["id"] = self.cand["id"] + "-r2"
        self.rev["proposal"]["skill_md"] = self.cand["proposal"]["skill_md"] + "\nRevision two.\n"
        self.ws.save_run({"run_id": "r1", "candidates": [self.cand, self.rev], "withheld": []})

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, cid, revises=None):
        promoter.stage(self.ws, cid, self.root, revises=revises)
        art = promoter.apply(self.ws, cid, "kai")
        art["applied_at"] = (fx.T0 - timedelta(days=1)).isoformat()   # live before the test's sessions
        self.ws.put(art)
        return art


class Uses(Base):
    def test_applied_capability_starts_on_trial(self):
        self.assertEqual(self.apply(self.cand["id"])["standing"], "trial")

    def test_a_session_counts_once_by_its_last_use(self):
        art = self.apply(self.cand["id"])
        us = standing.uses(art, corpus(("a", 1, [False, True]), ("b", 2, [True, False]), ("a", 3, [True])))
        self.assertEqual([(u.session, u.clean) for u in us], [("b", False), ("a", True)])

    def test_only_sessions_after_it_went_live(self):
        art = self.apply(self.cand["id"])
        art["applied_at"] = (fx.T0 + timedelta(days=5)).isoformat()
        self.assertEqual(standing.uses(art, corpus(("a", 1, [True]))), [])
        art["pending_merge"] = "https://example/pr/1"
        self.assertEqual(standing.uses(art, corpus(("z", 9, [True]))), [])

    def test_promote_and_demote(self):
        art = self.apply(self.cand["id"])
        three_clean = corpus(("a", 1, [True]), ("b", 2, [False, True]), ("c", 3, [True]))
        self.assertEqual(standing.settle(art, three_clean)["decision"], "promote")
        two_clean = corpus(("a", 1, [True]), ("b", 2, [True]))
        self.assertEqual(standing.settle(art, two_clean)["decision"], "hold")
        went_bad = corpus(("a", 1, [True]), ("b", 2, [False]), ("c", 3, [True, False]))
        self.assertEqual(standing.settle(art, went_bad)["decision"], "demote")
        one_bad = corpus(("a", 1, [True]), ("b", 2, [True]), ("c", 3, [False]))
        self.assertEqual(standing.settle(art, one_bad)["decision"], "hold")

    def test_thresholds_from_authority(self):
        art = self.apply(self.cand["id"])
        c = corpus(("a", 1, [True]))
        self.assertEqual(standing.settle(art, c, {"promote_after": 1})["decision"], "promote")

    def test_subagent_uses_are_invocations(self):
        art = {"kind": "subagent", "applied_at": (fx.T0 - timedelta(days=1)).isoformat(),
               "cases": [{"type": "agent_type", "name": "page-researcher"}]}
        c = Corpus()
        for i, (sess, status) in enumerate([("a", "completed"), ("b", "failed"), ("c", "orphaned")]):
            c.invocations.append(Invocation("claude", f"i{i}", sess, "page-researcher",
                                            fx.T0 + timedelta(days=i), None, status))
        self.assertEqual(standing.settle(art, c)["decision"], "demote")


class Review(Base):
    def test_promotion_is_recorded(self):
        self.apply(self.cand["id"])
        review.review(self.ws, corpus(("a", 1, [True]), ("b", 2, [True]), ("c", 3, [True])))
        self.assertEqual(self.ws.get(self.cand["id"])["standing"], "proven")
        self.assertIn("promote", [e["action"] for e in ledger.read(self.ws.ledger_path)])

    def test_demotion_is_a_review_verdict(self):
        self.apply(self.cand["id"])
        rows = review.review(self.ws, corpus(("a", 1, [False]), ("b", 2, [False])))
        self.assertEqual([r["verdict"] for r in rows], ["demote"])


class Demote(Base):
    def _digest(self, rows):
        run = {"run_id": "r2", "candidates": [], "withheld": []}
        d = digest.compose(self.ws, run, rows)
        return {"digest_id": "20261019T000000Z", "items": d["items"], "status": "awaiting_reply", "deliveries": []}

    def test_revision_supersedes_and_demotion_restores_it(self):
        parent = self.apply(self.cand["id"])
        skill = os.path.join(parent["target"], "SKILL.md")
        self.apply(self.rev["id"], revises=self.cand["id"])
        self.assertEqual(self.ws.get(self.cand["id"])["state"], "superseded")
        self.assertIn("Revision two.", open(skill).read())
        rows = review.review(self.ws, corpus(("a", 1, [False]), ("b", 2, [False])))
        st = self._digest(rows)
        self.assertEqual(st["items"][0]["kind"], "demotion")
        self.assertIn("restores", st["items"][0]["title"])
        res = digest.act(self.ws, st, "1", "kai", self.root)
        self.assertEqual(res["applied"], [1])
        self.assertEqual(self.ws.get(self.rev["id"])["state"], "retired")
        self.assertEqual(self.ws.get(self.cand["id"])["state"], "active")
        self.assertEqual(open(skill).read(), self.cand["proposal"]["skill_md"])
        self.assertIn("demote", [e["action"] for e in ledger.read(self.ws.ledger_path)])

    def test_declined_demotion_is_snoozed(self):
        self.apply(self.cand["id"])
        rows = review.review(self.ws, corpus(("a", 1, [False]), ("b", 2, [False])))
        st = self._digest(rows)
        digest.act(self.ws, st, "none", "kai", self.root)
        self.assertEqual(self.ws.get(self.cand["id"])["state"], "active")
        self.assertEqual(self._digest(rows)["items"], [])

    def test_grant_narrowing_has_no_standing(self):
        cand = {"kind": "subagent", "proposal": {"revises": "page-researcher", "grant": ["Bash"]}}
        self.assertTrue(promoter._grant_revision(cand))


if __name__ == "__main__":
    unittest.main()
