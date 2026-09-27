"""Analyze a round: python3 analyze.py results-round2.jsonl

The unit is the RUN, and the outcome is which way the agent reached the library:
  cli      only CLI calls
  inline   only inline programs
  both     both (the nudge's target case: inline first, then did it switch?)
  none     never touched the library (answered another way)

Arms B and C differ only after an inline call fires the nudge, so the skill's
effect is A vs B+C on first choice, and the nudge's effect is read only from C
runs where an inline call occurred. Fisher's exact test, two-sided, no deps.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from math import comb


def fisher_two_sided(a, b, c, d) -> float:
    """p-value for the 2x2 table [[a, b], [c, d]]."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def p(x):
        return comb(n1, x) * comb(n2, k - x) / comb(n, k)

    obs = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= obs * (1 + 1e-9)))


def outcome(r) -> str:
    i, c = r["inline_lib"], r["cli_lib"]
    return "both" if i and c else "inline" if i else "cli" if c else "none"


def main(path: str) -> None:
    rows = [json.loads(line) for line in open(path)]
    by = defaultdict(Counter)
    for r in rows:
        by[r["arm"]][outcome(r)] += 1
    print(f"{len(rows)} runs · correct {sum(r['correct'] for r in rows)}/{len(rows)} · "
          f"skill invoked {sum(r['skill_invoked'] for r in rows)} times")
    for arm in "ABC":
        n = sum(by[arm].values())
        print(f"  arm {arm}: n={n}  " + "  ".join(f"{k}={by[arm][k]}" for k in ("cli", "inline", "both", "none")))
    used = lambda arm: (by[arm]["cli"] + by[arm]["both"], by[arm]["inline"])  # noqa: E731
    a_cli, a_inl = used("A")
    bc_cli = used("B")[0] + used("C")[0]
    bc_inl = used("B")[1] + used("C")[1]
    print(f"\nskill present (B+C) vs absent (A), among runs that reached the library:")
    print(f"  CLI share  A {a_cli}/{a_cli + a_inl}   B+C {bc_cli}/{bc_cli + bc_inl}   "
          f"Fisher p = {fisher_two_sided(a_cli, a_inl, bc_cli, bc_inl):.3f}")
    for arm in "BC":
        c, i = used(arm)
        print(f"  arm {arm} alone: CLI {c}/{c + i}   vs A  p = {fisher_two_sided(a_cli, a_inl, c, i):.3f}")
    nudged = [r for r in rows if r["arm"] == "C" and r["inline_lib"]]
    print(f"\nnudge: C runs with an inline call = {len(nudged)}; of those, later CLI call in the same run = "
          f"{sum(1 for r in nudged if r['cli_lib'])}")
    by_task = defaultdict(lambda: defaultdict(Counter))
    for r in rows:
        by_task[r["task"]][r["arm"]][outcome(r)] += 1
    print("\nper task (cli/inline):")
    for t, arms in by_task.items():
        print(f"  {t:<18}" + "  ".join(f"{a}: {arms[a]['cli'] + arms[a]['both']}/{arms[a]['inline']}" for a in "ABC"))
    walls = {a: sorted(r["wall_s"] for r in rows if r["arm"] == a) for a in "ABC"}
    print("\nmedian wall s: " + "  ".join(f"{a} {w[len(w) // 2]}" for a, w in walls.items()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results-round2.jsonl")
