"""Dependency-free, conservative selection of harness experiments.

This module is advisory only: it never authorizes promotion or deployment.
"""
from __future__ import annotations

import math
from typing import Any


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Approximate 95% binomial confidence interval."""
    if trials <= 0 or not 0 <= successes <= trials:
        raise ValueError("successes must be between zero and positive trials")
    p = successes / trials
    d = 1 + z * z / trials
    c = (p + z * z / (2 * trials)) / d
    h = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / d
    return max(0.0, c - h), min(1.0, c + h)


def assess_candidate(
    control: dict[str, Any],
    treatment: dict[str, Any],
    *,
    noise_floor: float = 0.0,
    max_relative_cost_increase: float = 0.0,
    minimum_trials: int = 20,
) -> dict[str, Any]:
    """Conservative screening; not a replacement for RunTune's verify gate.

    Each arm: successes, trials, optional total_cost. Missing cost fails closed.
    Uses non-overlapping Wilson intervals as a conservative screening heuristic,
    NOT a formal paired test or proof of statistical significance.
    """
    if not 0 <= noise_floor <= 1 or max_relative_cost_increase < 0 or minimum_trials < 1:
        raise ValueError("invalid selection policy")
    for arm in (control, treatment):
        n, s = arm["trials"], arm["successes"]
        if not isinstance(n, int) or not isinstance(s, int) or n <= 0 or not 0 <= s <= n:
            raise ValueError("invalid trial counts")
    a, b = control, treatment
    control_rate = a["successes"] / a["trials"]
    treatment_rate = b["successes"] / b["trials"]
    delta = treatment_rate - control_rate
    reasons = []
    if min(a["trials"], b["trials"]) < minimum_trials:
        reasons.append("insufficient_trials")
    a_ci = wilson_interval(a["successes"], a["trials"])
    b_ci = wilson_interval(b["successes"], b["trials"])
    if b_ci[0] <= a_ci[1]:
        reasons.append("uncertain_quality_gain")
    if delta <= noise_floor:
        reasons.append("below_noise_floor")
    if "total_cost" not in a or "total_cost" not in b:
        reasons.append("cost_unavailable")
        relative_cost_change = None
    else:
        ac, bc = a["total_cost"], b["total_cost"]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (ac, bc)):
            raise ValueError("cost must be finite and nonnegative")
        per_control = ac / a["trials"]
        per_treatment = bc / b["trials"]
        relative_cost_change = ((per_treatment / per_control) - 1) if per_control else (0.0 if per_treatment == 0 else None)
        if relative_cost_change is None or relative_cost_change > max_relative_cost_increase:
            reasons.append("cost_budget_exceeded")
    return {
        "schema": "runtune.experiment-selection/1",
        "recommendation": "eligible_for_review" if not reasons else "hold",
        "reasons": reasons,
        "control_rate": control_rate,
        "treatment_rate": treatment_rate,
        "absolute_gain": delta,
        "control_wilson_95": list(a_ci),
        "treatment_wilson_95": list(b_ci),
        "relative_cost_change": relative_cost_change,
        "advisory_only": True,
    }
