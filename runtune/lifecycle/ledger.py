"""Append-only, hash-chained record of every lifecycle decision.

Each entry carries the hash of the one before it, so an edited or deleted line
breaks the chain at that point and `verify` names it. That proves a record was
altered; it does not prevent altering it. (Ported from callusguard's audit log.)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

GENESIS = "0" * 64


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(prev: str, event: dict) -> str:
    return hashlib.sha256((prev + _canon(event)).encode()).hexdigest()


def last_hash(path: str) -> str:
    try:
        with open(path) as f:
            lines = [l for l in f if l.strip()]
        return json.loads(lines[-1])["hash"] if lines else GENESIS
    except FileNotFoundError:
        return GENESIS


def append(path: str, event: dict) -> dict:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    prev = last_hash(path)
    entry = {"prev": prev, "event": event, "hash": _hash(prev, event)}
    with open(path, "a") as f:
        f.write(_canon(entry) + "\n")
    return entry


def read(path: str) -> list[dict]:
    try:
        with open(path) as f:
            return [json.loads(l)["event"] for l in f if l.strip()]
    except FileNotFoundError:
        return []


def verify(path: str) -> tuple[bool, str]:
    prev = GENESIS
    try:
        f = open(path)
    except FileNotFoundError:
        return True, "no ledger yet"
    with f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            e = json.loads(line)
            if e["prev"] != prev or e["hash"] != _hash(prev, e["event"]):
                return False, f"chain broken at line {n}"
            prev = e["hash"]
    return True, "chain intact"
