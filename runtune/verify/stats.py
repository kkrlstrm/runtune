"""The two numbers a verify verdict rests on, standard library only.

Fisher's exact test answers "did the treatment arm do the thing more often than control".
Interval overlap is the wrong test for that: at 3 runs per arm, 3/3 against 0/3 has
overlapping 95% intervals and a Fisher p of 0.10. `best_p` says, BEFORE any run is paid
for, the smallest p a given number of runs can produce, so a run count that cannot reach
significance is caught up front instead of reported as "inconclusive" afterwards.
"""

from __future__ import annotations

from math import comb, sqrt


def fisher(a: int, n1: int, b: int, n2: int) -> float:
    """Two-sided p for a/n1 vs b/n2 successes."""
    if n1 == 0 or n2 == 0:
        return 1.0
    k, n = a + b, n1 + n2

    def p(x):
        return comb(k, x) * comb(n - k, n1 - x) / comb(n, n1)

    obs = p(a)
    return min(1.0, sum(p(x) for x in range(max(0, k - n2), min(k, n1) + 1) if p(x) <= obs * (1 + 1e-9)))


def best_p(n: int) -> float:
    """The smallest p reachable with n runs per arm: perfect separation."""
    return fisher(n, n, 0, n)


def runs_for(alpha: float = 0.05) -> int:
    """Fewest runs per arm whose perfect separation clears alpha."""
    n = 1
    while best_p(n) >= alpha:
        n += 1
    return n


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)
