"""Receipts: what each approved digest item has already had done to it, so a re-run resumes.

An approval passes through steps that can each be interrupted: the item is applied to the
target, the target is committed on a branch and pushed, the branch becomes a pull request (or
a handoff). Before receipts, `inbox` applied every approved item and only then published; a
push that failed, or a machine that stopped in between, left artifacts marked `active` that
were never committed, and the next run on a server that resets its clone lost them. Nothing
errored, and the reply was then re-read with every item refused as "already active".

One file per digest, `.runtune/receipts/<digest>.json`, rewritten atomically after every step:

    {"digest": "...", "items": {"1": {"id": "...", "applied": "<ts>",
                                      "published": {"branch": "...", "ref": "...", "at": "<ts>"}}}}

A step is recorded after it happened, never before, so a receipt is evidence. `resume`
finds every applied-but-unpublished item and carries it the rest of the way. Adapted from
@parcha/pi-agent-learn's run receipts and its idempotent propose step ("already").
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Receipts:
    def __init__(self, ws, digest_id: str):
        self.dir = os.path.join(ws.root, "receipts")
        self.path = os.path.join(self.dir, f"{digest_id}.json")
        self.digest_id = digest_id
        self.doc = {"digest": digest_id, "items": {}}
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.doc = json.load(f)

    def item(self, n) -> dict:
        return self.doc["items"].get(str(n), {})

    def mark(self, n, step: str, artifact_id: str | None = None, **data) -> None:
        it = self.doc["items"].setdefault(str(n), {})
        if artifact_id:
            it["id"] = artifact_id
        it[step] = {"at": _now(), **data} if data else _now()
        self._write()

    def unpublished(self) -> list[tuple[int, str]]:
        return [(int(n), it["id"]) for n, it in sorted(self.doc["items"].items(), key=lambda kv: int(kv[0]))
                if it.get("applied") and not it.get("published") and it.get("id")]

    def _write(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(self.doc, f, indent=2)
        os.replace(tmp, self.path)


def all_digests(ws) -> list[str]:
    d = os.path.join(ws.root, "receipts")
    if not os.path.isdir(d):
        return []
    return sorted(fn[:-5] for fn in os.listdir(d) if fn.endswith(".json") and not fn.startswith("."))
