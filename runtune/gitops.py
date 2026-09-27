"""Apply-by-pull-request: how a RunTune running on a server changes a repo it does not own.

On a VM the targets (skills, agents, rulesets, routes) live in a git repository, not on
the machine. `runtune inbox --git` applies approved artifacts into a clone at
--target-root, commits them on a branch, pushes, and opens a pull request. The merge is
the final human approval, and `git revert` is the undo.

Needs: the clone's `origin` pointing at the repo, $RUNTUNE_GIT_TOKEN (a token that can
push branches and open PRs) and $RUNTUNE_GIT_REPO (`owner/name`, GitHub).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request


def _git(root: str, *args: str) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True).stdout.strip()


def publish(root: str, digest_id: str, body: str) -> str:
    branch = f"runtune/{digest_id.lower()}"
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "checkout", "-b", branch)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=runtune", "-c", "user.email=runtune@localhost", "commit", "-m",
         f"RunTune: apply approvals from digest {digest_id}\n\n{body}")
    _git(root, "push", "-u", "origin", branch)
    _git(root, "checkout", base)
    # The clone stays on the base branch, without the change: the target only
    # contains it once the PR is merged and pulled, which is what review checks.
    _git(root, "branch", "-D", branch)
    repo, token = os.environ.get("RUNTUNE_GIT_REPO"), os.environ.get("RUNTUNE_GIT_TOKEN")
    if not (repo and token):
        return f"pushed {branch}; set RUNTUNE_GIT_REPO and RUNTUNE_GIT_TOKEN to open the PR automatically"
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/pulls", method="POST",
        data=json.dumps({"title": f"RunTune: approved changes ({digest_id})", "head": branch, "base": base,
                         "body": body + "\n\nApproved by reply to the RunTune digest. Merging applies it."}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["html_url"]
