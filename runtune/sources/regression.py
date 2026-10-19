"""Regression results: does each cleared mode still pass its frozen spec?

model-eval-gate's eval/regression.ts re-runs every mode's spec against the model the
allowlist routes it to and writes `regression-results.json`. That is the maintained
half of a clearance: `verified_date` says when a mode was cleared, the latest
regression run says whether it still holds. RunTune reads it so a drift becomes a
finding in the same digest as everything else, and a clean run can re-clear a mode
whose `verified_date` has gone stale.

Two writers exist and both are read:

    model-eval-gate   {"ran_at", "results": [{"mode", "model", "meanScore", "passHatK",
                       "baseline", "signals": [{"type", "detail"}], "inconclusive", ...}]}
    older harnesses   the same, with "drift": [...] in place of "signals"

Signal types split in two. MODEL signals say the model got worse or changed; INFRA
signals say the run could not judge it (the provider was down, the output did not
parse, a grader threw). Only model signals are drift.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

MODEL_SIGNALS = {"model_quality_drift", "consistency", "model_swap", "drift"}
INFRA_SIGNALS = {"provider_unavailable", "parse_failure", "grader_failure"}
FILENAME = "regression-results.json"


def discover(routes_path: str | None) -> str | None:
    """A results file next to the allowlist, where regression writes it."""
    if not routes_path:
        return None
    base = os.path.dirname(os.path.abspath(os.path.expanduser(routes_path)))
    for p in (os.path.join(base, FILENAME), os.path.join(base, "eval", FILENAME)):
        if os.path.exists(p):
            return p
    return None


def _signals(r: dict) -> list[dict]:
    raw = r.get("signals")
    if raw is None:
        raw = r.get("drift") or []
    out = []
    for s in raw:
        if isinstance(s, dict):
            out.append({"type": str(s.get("type") or "drift"), "detail": str(s.get("detail") or "")})
        else:
            out.append({"type": "drift", "detail": str(s)})
    return out


def load(path: str) -> dict:
    """-> {"path", "ran_at", "modes": {mode: result}}. Each result carries normalized
    `signals`, `model_drift` (model signals only), `inconclusive` and `clean`."""
    with open(os.path.expanduser(path)) as f:
        data = json.load(f)
    ran_at = datetime.fromisoformat(str(data.get("ran_at")).replace("Z", "+00:00"))
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=timezone.utc)
    modes = {}
    for r in data.get("results") or []:
        if not isinstance(r, dict) or not r.get("mode"):
            continue
        sig = _signals(r)
        inconclusive = bool(r.get("inconclusive")) or any(s["type"] == "provider_unavailable" for s in sig)
        model_drift = [s for s in sig if s["type"] in MODEL_SIGNALS or s["type"] not in INFRA_SIGNALS]
        modes[r["mode"]] = {
            "mode": r["mode"], "model": r.get("model"), "ran_at": ran_at,
            "mean_score": r.get("meanScore"), "pass_hat_k": r.get("passHatK"),
            "baseline": r.get("baseline") or {}, "signals": sig,
            "model_drift": [] if inconclusive else model_drift,
            "inconclusive": inconclusive,
            "clean": not sig and not inconclusive,
        }
    return {"path": path, "ran_at": ran_at, "modes": modes}
