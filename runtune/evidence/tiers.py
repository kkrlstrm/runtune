"""How much a cluster of runs is allowed to claim.

Two gradings, one for each side of the loop, and both refuse to let a count stand
in for a rate.

FAILURE TIERS (constraints) — ported unchanged from callusguard

    deterministic   >= 95% of attempts failed, over >= MIN_ATTEMPTS   may block
    reproducible    >= 50%                                             may deny
    probabilistic   < 50%, usually works                               may nudge
    anecdotal       too few attempts at any rate                       not proposed
    unknown         no denominator available                           may nudge

REUSE TIERS (capabilities, subagents, routes)

A success-side pattern is worth a capability when the same need recurs across
independent occasions — not when one long session repeats itself 400 times. So
the reuse grade is on BREADTH (distinct sessions, distinct weeks), and volume
only breaks ties:

    established     >= 20 sessions across >= 4 weeks
    recurring       >= 5 sessions across >= 2 weeks
    local           everything else: one burst, one session, one week  not proposed

Both gradings are OBSERVATIONAL — rates over the traffic that happened to run,
never re-run under control. Read a tier as a prior, not a proof.
"""

from __future__ import annotations

DETERMINISTIC = "deterministic"
REPRODUCIBLE = "reproducible"
PROBABILISTIC = "probabilistic"
ANECDOTAL = "anecdotal"
UNKNOWN = "unknown"

MIN_ATTEMPTS = 5
DETERMINISTIC_AT = 0.95
REPRODUCIBLE_AT = 0.50

ACTION_RANK = {"monitor": 0, "nudge": 1, "deny": 2, "block": 3}

TIER_ACTION_CEILING = {
    DETERMINISTIC: "block",
    REPRODUCIBLE: "deny",
    PROBABILISTIC: "nudge",
    ANECDOTAL: "monitor",
    UNKNOWN: "nudge",
}

ESTABLISHED = "established"
RECURRING = "recurring"
LOCAL = "local"

REUSE_THRESHOLDS = {ESTABLISHED: (20, 4), RECURRING: (5, 2)}


def classify_failure(fails, attempts, min_attempts: int = MIN_ATTEMPTS):
    """(tier, rate). A missing denominator is UNKNOWN, never an invented rate."""
    try:
        f, a = int(fails or 0), int(attempts or 0)
    except (TypeError, ValueError):
        return UNKNOWN, None
    if a <= 0:
        return UNKNOWN, None
    rate = min(1.0, f / a)
    if a < min_attempts:
        return ANECDOTAL, rate
    if rate >= DETERMINISTIC_AT:
        return DETERMINISTIC, rate
    if rate >= REPRODUCIBLE_AT:
        return REPRODUCIBLE, rate
    return PROBABILISTIC, rate


def ceiling(tier: str) -> str:
    """Unrecognised tiers get the strictest cap: an unknown label never widens authority."""
    return TIER_ACTION_CEILING.get(tier, "monitor")


def within_ceiling(action: str, tier: str) -> bool:
    return ACTION_RANK.get(action, 99) <= ACTION_RANK[ceiling(tier)]


def classify_reuse(sessions: int, weeks: int) -> str:
    for tier in (ESTABLISHED, RECURRING):
        s, w = REUSE_THRESHOLDS[tier]
        if sessions >= s and weeks >= w:
            return tier
    return LOCAL


def describe_failure(tier, rate, attempts) -> str:
    if rate is not None and attempts:
        return f"{tier} ({round(rate * 100)}% of {attempts} attempts)"
    return tier
