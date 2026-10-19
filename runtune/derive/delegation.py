"""WORK THE FRONTIER MODEL DOES -> a cheaper model, once an eval says so.

The route deriver looks at traffic that already goes through the router. This looks at
the work the coding agents do themselves (a script run hundreds of times, the same job
re-typed every week) and asks model-eval-gate, RunTune's route engine, whether any of it
could move to a cheaper model. model-eval-gate's workload classes, eligibility rules and
billing counterfactual are used as they are; this module only turns its answer into
findings. Without model-eval-gate installed it reports that it did not run.

What it can and cannot find:

  * BILLING decides whether moving work saves money. Work on a subscription has ~$0
    marginal cost, so moving it buys rate-limit headroom, not dollars, and it is
    reported as such, in one line, never as a saving. The coding agents' own turns are
    taken as subscription-billed unless $RUNTUNE_HOST_BILLING=api says otherwise.
  * STAKES cannot be read from telemetry: nothing in a call record says whether a person
    reads one output and acts on it. model-eval-gate treats unknown stakes as high, so
    API-billed work it would otherwise clear becomes a finding that asks that question.
  * Only named tasks count: a script, or a repeated sub-agent job. A bare command head
    (`git`, `ls`) is a habit, not a task a model could take over.
"""

from __future__ import annotations

import os
import tempfile

from ..evidence import tiers
from .candidate import WIDEN, Candidate

MIN_RUNS = 30


def derive(corpus, host_billing: str | None = None) -> tuple[list, list]:
    host_billing = host_billing or os.environ.get("RUNTUNE_HOST_BILLING", "subscription")
    try:
        from meg.observe.runtune_source import rows_from_corpus
        from meg.store import Store
        from meg.workload.cluster import from_harness
    except ImportError:
        c = Candidate(kind="route", key="delegation|unavailable",
                      title="delegation: not checked — model-eval-gate is not installed",
                      claim="which of the agents' own work could move to a cheaper model is model-eval-gate's "
                            "question; install it (runtune[gate]) to have it asked",
                      tier="history", sources=["runtune"])
        c.gate("model-eval-gate", False, "not importable")
        return [], [c]

    work, calls = rows_from_corpus(corpus, host_billing)
    if not work:
        return [], []
    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "work.db"))
        store.upsert_work(work)
        classes = [c for c in from_harness(store, min_n=MIN_RUNS)
                   if c.evidence.get("kind") in ("script", "subagent")]

    out, withheld, free = [], [], []
    for cls in classes:
        if cls.billing == "subscription":
            free.append(cls)
            continue
        other = [b for b in cls.blockers if not b.startswith("stakes unconfirmed")]
        c = Candidate(
            kind="route", key=f"delegation|{cls.label}",
            title=f"delegation: `{cls.label}` ran {cls.n:,} times, {cls.billing}-billed — "
                  "confirm its stakes before an eval",
            claim=("model-eval-gate finds this work eligible to move to a cheaper model except for one question "
                   "telemetry cannot answer: does a person act on a single output? If not, scaffold an eval "
                   f"(`meg eval scaffold {cls.label}`) and run a bake-off; the route changes only on a pass."),
            tier=tiers.RECURRING, direction=WIDEN, sources=sorted(cls.evidence.get("harnesses", [])),
            numbers={"runs": cls.n, "billing": cls.billing, "sessions": cls.evidence.get("sessions"),
                     "models_seen": cls.models_seen, "blockers": cls.blockers},
            requires=["human-review", "eval"],
            ladder=[("route", True, "an eval-cleared mode moves this work; nothing moves without one")],
            caveats=["a run count is not a cost: the counterfactual needs prices, from `meg workload propose`"])
        c.gate("eligibility", not other, "; ".join(other) or "only the stakes question is open")
        (out if c.admitted else withheld).append(c)

    if free:
        runs = sum(c.n for c in free)
        f = Candidate(
            kind="route", key="delegation|subscription",
            title=f"delegation: {len(free)} repeated task(s), {runs:,} runs, are subscription-billed — "
                  "moving them saves $0",
            claim=("subscription work has ~$0 marginal cost: moving it to a cheaper model buys rate-limit "
                   "headroom and throughput, not dollars. Largest: "
                   + ", ".join(f"`{c.label}` ({c.n:,})" for c in sorted(free, key=lambda c: -c.n)[:5])),
            tier="history", sources=["runtune"], numbers={"tasks": len(free), "runs": runs,
                                                          "host_billing": host_billing})
        f.gate("billing", False, "subscription-billed: no dollar saving to propose")
        withheld.append(f)
    return out, withheld
