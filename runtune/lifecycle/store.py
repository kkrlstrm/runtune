"""The workspace: where proposals wait, and where adopted artifacts are tracked.

    .runtune/
      candidates/<run>.json     every derive run, admitted AND withheld
      evidence/<sha8>.md        content-addressed, redacted evidence — never named by a model
      artifacts/<id>.json       one record per staged/active/retired artifact
      drafts/<id>/              the files an artifact would write, for a human to read
      ledger.jsonl              hash-chained lifecycle decisions
      authority.json            what may be applied, by whom, in which direction

Artifact states:  staged -> active -> retired
                        \\-> rejected
An artifact records the digest of its target file when it was staged; apply
refuses if the target changed since (AutoRefine's stale-parent refusal), because
a proposal reviewed against one version of a file is not a review of the next.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from ..evidence.redact import redact
from . import ledger

DEFAULT_AUTHORITY = {
    "_readme": "What RunTune may write. The learner proposes; a named human applies. "
               "Widening (a looser constraint, a broader grant, a new model on the allowlist) "
               "is never applied without --approve AND a reason; routes also need --eval.",
    "targets": {
        "constraint": "rules/runtune.rules.json",
        "capability": ".claude/skills",
        "subagent": ".claude/agents",
        "route": "routes.json",
    },
    "protected": [".claude/settings.json", ".claude/settings.local.json", ".codex/config.toml",
                  "hooks/", ".git/"],
    "auto_apply": [],
}


class Workspace:
    def __init__(self, root: str = ".runtune"):
        self.root = root
        for d in ("candidates", "evidence", "artifacts", "drafts"):
            os.makedirs(os.path.join(root, d), exist_ok=True)
        ap = os.path.join(root, "authority.json")
        if not os.path.exists(ap):
            with open(ap, "w") as f:
                json.dump(DEFAULT_AUTHORITY, f, indent=2)

    # ------------------------------------------------------------------ paths
    @property
    def ledger_path(self) -> str:
        return os.path.join(self.root, "ledger.jsonl")

    def authority(self) -> dict:
        with open(os.path.join(self.root, "authority.json")) as f:
            return json.load(f)

    def policy_digest(self) -> str:
        """Digest of the authority file: the rules every artifact was admitted under."""
        return file_digest(os.path.join(self.root, "authority.json")) or ""

    # --------------------------------------------------------------- evidence
    def put_evidence(self, text: str) -> str:
        text = redact(text)
        h = hashlib.sha256(text.encode()).hexdigest()[:8]
        rel = f"evidence/{h}.md"
        p = os.path.join(self.root, rel)
        if not os.path.exists(p):
            with open(p, "w") as f:
                f.write(text)
        return rel

    # -------------------------------------------------------------- candidates
    def save_run(self, run: dict) -> str:
        run_id = run.get("run_id") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run["run_id"] = run_id
        p = os.path.join(self.root, "candidates", f"{run_id}.json")
        with open(p, "w") as f:
            json.dump(run, f, indent=2, default=str)
        ledger.append(self.ledger_path, {"action": "derive", "run_id": run_id,
                                         "admitted": len(run.get("candidates", [])),
                                         "withheld": len(run.get("withheld", []))})
        return p

    def latest_run(self) -> dict | None:
        d = os.path.join(self.root, "candidates")
        runs = sorted(f for f in os.listdir(d) if f.endswith(".json"))
        if not runs:
            return None
        with open(os.path.join(d, runs[-1])) as f:
            return json.load(f)

    def find_candidate(self, cid: str) -> dict | None:
        d = os.path.join(self.root, "candidates")
        for fn in sorted(os.listdir(d), reverse=True):
            with open(os.path.join(d, fn)) as f:
                run = json.load(f)
            for c in run.get("candidates", []) + run.get("withheld", []):
                if c["id"] == cid:
                    return c
        return None

    # --------------------------------------------------------------- artifacts
    def artifact_path(self, aid: str) -> str:
        return os.path.join(self.root, "artifacts", f"{aid}.json")

    def get(self, aid: str) -> dict | None:
        try:
            with open(self.artifact_path(aid)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def put(self, art: dict) -> None:
        with open(self.artifact_path(art["id"]), "w") as f:
            json.dump(art, f, indent=2, default=str)

    def artifacts(self, state: str | None = None) -> list[dict]:
        d = os.path.join(self.root, "artifacts")
        out = []
        for fn in sorted(os.listdir(d)):
            with open(os.path.join(d, fn)) as f:
                a = json.load(f)
            if state is None or a.get("state") == state:
                out.append(a)
        return out


def file_digest(path: str) -> str | None:
    """Digest of a file, or of every file under a directory (skills are directories).
    None means the target does not exist yet."""
    if os.path.isdir(path):
        h = hashlib.sha256()
        for base, dirs, files in sorted(os.walk(path)):
            dirs.sort()
            for fn in sorted(files):
                p = os.path.join(base, fn)
                h.update(os.path.relpath(p, path).encode())
                with open(p, "rb") as f:
                    h.update(f.read())
        return h.hexdigest()
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None
