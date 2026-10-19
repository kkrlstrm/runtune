"""Apply-by-pull-request: how a RunTune running on a server changes a repo it does not own.

On a VM the targets (skills, agents, rulesets, routes) live in a git repository, not on
the machine. `runtune inbox --git` applies approved artifacts into a clone at
--target-root, commits them on a branch, pushes, and opens a pull request. The merge is
the final human approval, and `git revert` is the undo.

Before anything is committed, the staged diff and the PR body go through the write guard
(`runtune/guard.py`) with the clone's own authority config; a hit aborts the publish and
leaves the clone on its base branch with nothing pushed.

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


def staged_texts(root: str) -> dict[str, str]:
    """{path: added text} for everything `git add -A` would commit."""
    out = {}
    names = _git(root, "diff", "--cached", "--name-only", "-z").split("\0")
    for name in filter(None, names):
        diff = subprocess.run(["git", "-C", root, "diff", "--cached", "--no-color", "-U0", "--", name],
                              check=True, capture_output=True).stdout.decode("utf-8", "replace")
        out[os.path.join(root, name)] = "\n".join(l[1:] for l in diff.splitlines()
                                                 if l.startswith("+") and not l.startswith("+++"))
    return out


def branch_for(digest_id: str) -> str:
    return f"runtune/{digest_id.lower()}"


def remote_ref(root: str, branch: str) -> str | None:
    """The commit `origin` holds for `branch`, or None."""
    out = _git(root, "ls-remote", "--heads", "origin", branch)
    return out.split()[0] if out else None


def publish(root: str, digest_id: str, body: str, guard_cfg: dict | None = None) -> str:
    """Commit everything staged in `root` on runtune/<digest>, push it, open the PR.

    Idempotent: a branch `origin` already holds for this digest is the earlier run's publish,
    so it is reported, not pushed again (a re-run after an interrupted publish heals instead of
    duplicating). A local branch left by an interrupted run is replaced."""
    from . import guard as write_guard
    branch = branch_for(digest_id)
    pushed = remote_ref(root, branch)
    if pushed:
        return f"already pushed {branch} @ {pushed[:12]}"
    base = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "checkout", "-q", "-B", branch)
    _git(root, "add", "-A")
    texts = staged_texts(root)
    texts["message"] = body
    try:
        refusals = write_guard.Guard(guard_cfg, root).check(texts)
    except FileNotFoundError as exc:
        refusals = [write_guard.Refusal("guard_config", "authority.json", str(exc))]
    if refusals:
        _git(root, "reset", "-q")
        _git(root, "checkout", "-q", base)
        _git(root, "branch", "-D", branch)
        raise write_guard.GuardRefused(refusals)
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
