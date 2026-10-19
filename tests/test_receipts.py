"""Receipts: an approval interrupted between apply and publish is resumed, never lost.

The failure this fixes: `inbox --git` applied every approved item, then published. A push that
failed (or a machine that stopped) left artifacts `active` that no branch held; a server that
resets its clone each run then lost them, and the reply was re-read with every item refused as
"already active". These hold:
- the digest is saved as answered before the publish starts;
- an unpublished artifact is marked, and review does not measure it;
- `inbox --git` resumes it: re-applies through every apply check when the target was reset,
  publishes, and records the branch;
- a reply re-read after an interruption reports "already applied", never re-applies;
- publishing a digest whose branch `origin` already holds is reported, not pushed twice.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest

from runtune import cli, gitops
from runtune.derive import capabilities
from runtune.lifecycle import receipts, review
from runtune.lifecycle.store import Workspace
from runtune.notify import digest

from . import fixtures as fx

DID = "20261019T080000Z"


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, text=True).stdout.strip()


class Interrupted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "target")
        self.bare = os.path.join(self.tmp.name, "origin.git")
        os.makedirs(self.root)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        subprocess.run(["git", "init", "-q", "--bare", self.bare], check=True)
        self.ws = Workspace(os.path.join(self.tmp.name, ".runtune"))
        caps, _ = capabilities.derive_inline(fx.inline_ttt())
        self.cand = caps[0].to_dict()
        self.ws.save_run({"run_id": "r1", "candidates": [self.cand], "withheld": []})
        self.state = {"digest_id": DID, "status": "awaiting_reply", "deliveries": [],
                      "items": [{"n": 1, "id": self.cand["id"], "direction": self.cand["direction"],
                                 "kind": self.cand["kind"], "title": self.cand["title"]}]}
        self.args = argparse.Namespace(workspace=self.ws.root, target_root=self.root, git=True, approver="kai")

    def tearDown(self):
        self.tmp.cleanup()

    def _reset_target(self):
        """What a server does each run: the clone goes back to the baked harness."""
        git(self.root, "reset", "-q", "--hard")
        git(self.root, "clean", "-qfd")

    def _approve_and_fail_the_push(self):
        res = digest.act(self.ws, self.state, "1", "kai", self.root)
        self.assertEqual(res["applied"], [1])
        # No `origin` yet: the publish fails the way a failed push does.
        with self.assertRaises(subprocess.CalledProcessError):
            cli._after_done(self.args, self.state, res)
        return res

    def test_interrupted_publish_is_marked_and_not_measured(self):
        self._approve_and_fail_the_push()
        rc = receipts.Receipts(self.ws, DID)
        self.assertEqual(rc.unpublished(), [(1, self.cand["id"])])
        art = self.ws.get(self.cand["id"])
        self.assertEqual(art["pending_publish"], DID)
        got = review.review(self.ws, fx.inline_ttt())
        self.assertEqual([r["verdict"] for r in got if r["id"] == self.cand["id"]], ["pending"])

    def test_resume_after_the_target_was_reset(self):
        self._approve_and_fail_the_push()
        self._reset_target()
        git(self.root, "remote", "add", "origin", self.bare)
        self.assertEqual(cli._resume(self.args), [DID])
        branch = gitops.branch_for(DID)
        self.assertTrue(gitops.remote_ref(self.root, branch))
        files = subprocess.run(["git", "--git-dir", self.bare, "ls-tree", "-r", "--name-only", branch],
                               capture_output=True, text=True).stdout
        self.assertIn("SKILL.md", files)
        rc = receipts.Receipts(self.ws, DID)
        self.assertEqual(rc.unpublished(), [])
        self.assertEqual(rc.item(1)["published"]["branch"], branch)
        art = self.ws.get(self.cand["id"])
        self.assertNotIn("pending_publish", art)
        self.assertTrue(art["pending_merge"])
        # Nothing left to resume, and running it again changes nothing.
        self.assertEqual(cli._resume(self.args), [])

    def test_a_reread_reply_reports_already_applied(self):
        self._approve_and_fail_the_push()
        again = digest.act(self.ws, self.state, "1", "kai", self.root)
        self.assertEqual(again["applied"], [])
        self.assertEqual(again["already"], [1])
        self.assertEqual(again["refused"], [])

    def test_apply_without_its_receipt_is_recognized(self):
        res = digest.act(self.ws, self.state, "1", "kai", self.root)
        rc = receipts.Receipts(self.ws, DID)
        del rc.doc["items"]["1"]["applied"]          # stopped between apply and its receipt
        rc._write()
        again = digest.act(self.ws, self.state, "1", "kai", self.root)
        self.assertEqual(again["already"], [1])
        self.assertTrue(receipts.Receipts(self.ws, DID).item(1).get("applied"))
        self.assertEqual(res["applied"], [1])

    def test_publish_twice_is_reported_not_pushed_twice(self):
        git(self.root, "remote", "add", "origin", self.bare)
        res = digest.act(self.ws, self.state, "1", "kai", self.root)
        cli._after_done(self.args, self.state, res)
        first = gitops.remote_ref(self.root, gitops.branch_for(DID))
        msg = gitops.publish(self.root, DID, "again", {})
        self.assertTrue(msg.startswith("already pushed"))
        self.assertEqual(gitops.remote_ref(self.root, gitops.branch_for(DID)), first)


if __name__ == "__main__":
    unittest.main()
