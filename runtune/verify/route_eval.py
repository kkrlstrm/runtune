"""The evidence a route change must carry: a model eval about THIS mode, THIS candidate and
THIS incumbent.

RunTune does not run model evals; the harness that scores a mode does. Until this module,
`apply --eval` on a route accepted any file that existed, so `{}` admitted a model to the
allowlist. A route eval result is now a small JSON document the eval harness writes:

    {
      "schema": "runtune.route-eval/1",
      "mode": "extract",                    the mode the proposal changes
      "candidate": "qwen/cheap",            the model being admitted
      "incumbent": "deepseek/v4-flash",     the model it replaces, scored on the same cases
      "verdict": "pass",                    the harness's own decision
      "cases": 40,                          distinct inputs scored
      "metric": "field F1 vs gold",
      "candidate_score": 0.91,
      "incumbent_score": 0.90,
      "non_inferiority_margin": 0.02,       optional: how far below the incumbent still passes
      "created": "2026-10-08T12:00:00Z",
      "harness": "evals/extract.py"         optional: what produced it
    }

Re-clearing a stale mode uses the same document with `candidate` set to the model the mode
already runs; `incumbent` and `incumbent_score` may then be omitted.

`check` refuses a result about another mode or model, one that is older than the policy
allows, one with too few cases, one whose verdict is not `pass`, and one that passed a
candidate scoring below the incumbent by more than a declared margin. The harness decides
pass or fail; RunTune checks that the decision is about this change and that the numbers
it reports do not contradict it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..derive.routes import _model_eq

SCHEMA = "runtune.route-eval/1"
DEFAULTS = {"min_cases": 20, "max_age_days": 30}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def digest(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check(req: dict, eval_ref: str, policy: dict | None = None, now: datetime | None = None) -> list:
    """Problems with using `eval_ref` as the eval behind `req` (a route proposal's
    `eval_request`: mode, candidate, incumbent). Empty list: usable. Never raises on a bad
    file; it reports it."""
    policy = {**DEFAULTS, **(policy or {})}
    now = now or datetime.now(timezone.utc)
    try:
        with open(eval_ref) as f:
            r = json.load(f)
    except (OSError, ValueError) as e:
        return [f"{eval_ref} is not a readable eval result ({type(e).__name__})"]
    if not isinstance(r, dict) or r.get("schema") != SCHEMA:
        return [f"{eval_ref} is not a route eval result (needs \"schema\": \"{SCHEMA}\"; see docs/VERIFY.md)"]
    problems = []
    if r.get("mode") != req["mode"]:
        problems.append(f"it evaluated mode `{r.get('mode')}`, not `{req['mode']}`")
    if not _model_eq(r.get("candidate"), req["candidate"]):
        problems.append(f"it evaluated `{r.get('candidate')}`, not the candidate `{req['candidate']}`")
    if req.get("incumbent") and not _model_eq(r.get("incumbent"), req["incumbent"]):
        problems.append(f"it compared against `{r.get('incumbent')}`, not the model this replaces "
                        f"(`{req['incumbent']}`)")
    if r.get("verdict") != "pass":
        problems.append(f"the verdict is `{r.get('verdict')}`, not `pass`")
    cases = r.get("cases")
    if not isinstance(cases, int) or isinstance(cases, bool) or cases < policy["min_cases"]:
        problems.append(f"it scored {cases!r} cases; the policy needs at least {policy['min_cases']}")
    cs, inc = _num(r.get("candidate_score")), _num(r.get("incumbent_score"))
    if not req.get("incumbent"):
        # re-clearing the model a mode already runs: there is nothing to compare against
        if cs is None:
            problems.append("it must report candidate_score, so the decision can be audited")
    elif cs is None or inc is None:
        problems.append("it must report candidate_score and incumbent_score, so the decision can be audited")
    else:
        margin = _num(r.get("non_inferiority_margin")) or 0.0
        if cs < inc - margin:
            problems.append(f"it passed a candidate that scored below the incumbent ({cs:g} vs {inc:g}"
                            + (f", margin {margin:g})" if margin else "; no non_inferiority_margin declared)"))
    try:
        created = datetime.fromisoformat(str(r.get("created")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds() / 86400
        if age < -1:
            problems.append(f"it is dated in the future ({r.get('created')})")
        elif age > policy["max_age_days"]:
            problems.append(f"it is {age:.0f} days old; the policy accepts {policy['max_age_days']}")
    except ValueError:
        problems.append(f"`created` is not an ISO timestamp ({r.get('created')!r})")
    return problems
