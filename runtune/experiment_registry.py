"""Append-only experiment hypothesis log with chained integrity hashes.

This log is decision support, not an approval channel. Keep it separate from
RunTune's authoritative approval ledger.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_history(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    records = []
    previous = "0" * 64
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        digest = record.pop("hash")
        if record.get("previous_hash") != previous or hashlib.sha256(_canonical(record)).hexdigest() != digest:
            raise ValueError("experiment history integrity check failed")
        record["hash"] = digest
        records.append(record)
        previous = digest
    return records


def append_hypothesis(path: str | Path, *, hypothesis_id: str, hypothesis: str,
                      candidate_sha: str, verdict: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if not hypothesis_id or not hypothesis or not candidate_sha or verdict not in {
        "proposed", "rejected", "inconclusive", "passed", "retired"
    }:
        raise ValueError("invalid hypothesis entry")
    path = Path(path)
    history = read_history(path)
    if history and any(r["hypothesis_id"] == hypothesis_id and r["candidate_sha"] == candidate_sha
                       and r["verdict"] == verdict for r in history):
        raise ValueError("duplicate hypothesis verdict")
    record = {
        "schema": "runtune.experiment-history/1",
        "hypothesis_id": hypothesis_id,
        "hypothesis": hypothesis,
        "candidate_sha": candidate_sha,
        "verdict": verdict,
        "evidence": evidence,
        "previous_hash": history[-1]["hash"] if history else "0" * 64,
    }
    record["hash"] = hashlib.sha256(_canonical(record)).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return record
