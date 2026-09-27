"""Measure every active artifact against the traffic that came after it.

The verdicts, shared across all four kinds so one report reads them all:

    keep         doing what it was adopted for
    review       measurably making things WORSE, or firing so often the workflow
                 underneath should be fixed instead — rewrite it
    probe        went quiet after it was adopted. Fixed, or bypassed? Those are
                 opposite facts with the same signature; replay it before deciding
    retire       nothing uses it: a capability with no adoption, a subagent type
                 nobody spawns, a constraint that has never matched anything
    revalidate   the model underneath changed after its evidence was gathered;
                 what it learned may no longer be true of the model now running it
    pending      too little traffic since adoption to say anything

Review never changes anything. Every verdict is a proposal for `retire` or for a
new `derive`, which then goes through the same promoter as everything else.
Shrinking the library is the loop working, not rotting.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import measure
from ..derive import constraints as dc


def review(ws, corpus, days: int = 28) -> list[dict]:
    eps = measure.epochs(corpus)
    out = []
    for art in ws.artifacts("active"):
        at = datetime.fromisoformat(art["applied_at"])
        verdict, detail = _judge(art, corpus, at, days)
        evidence_end = art["candidate"].get("numbers", {}).get("last") or art.get("staged_at")
        later = [e for e in eps if evidence_end and e["week"] > _week(evidence_end)]
        if later and verdict in ("keep", "pending"):
            verdict = "revalidate"
            detail["model_change"] = later[-1]
        if art.get("policy_digest") and art["policy_digest"] != ws.policy_digest() and verdict in ("keep", "pending"):
            verdict = "revalidate"
            detail["policy_change"] = "authority.json changed since this was admitted"
        out.append({"id": art["id"], "kind": art["kind"], "title": art["title"],
                    "applied_at": art["applied_at"], "verdict": verdict, "detail": detail})
    return out


def _week(iso: str) -> str:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _judge(art, corpus, at, days):
    since_days = (datetime.now(timezone.utc) - at).days
    for case in art.get("cases", []):
        t = case.get("type")
        if t == "pattern":
            rx = re.compile(case["pattern"])
            match = lambda e: e.surface in dc.SHELL_SURFACES and bool(rx.search(e.text))  # noqa: E731
            ba = measure.before_after(corpus, match, at, days=days)
            after = ba["after"]["target"]["attempts"]
            ever = sum(1 for e in corpus.events if match(e))
            if ever == 0:
                return "retire", {"why": "has never matched a recorded attempt", **ba}
            if after == 0 and since_days >= 14:
                return "probe", {"why": "quiet since adoption — fixed or bypassed? replay before pruning", **ba}
            if after < 5:
                return "pending", ba
            if (ba.get("net_change") or 0) > 0.02:
                return "review", {"why": "failure rate rose beyond the control after adoption", **ba}
            return "keep", ba
        if t == "adoption":
            root = case["root"]
            old = lambda e: (e.inline_target or "").split(":")[0] == root  # noqa: E731
            new = lambda e: not e.inline_target and root in {s[:-3] if s.endswith(".py") else s  # noqa: E731
                                                             for s in e.shape.split(" ")}
            ad = measure.adoption(corpus, old, new, at=at)
            if (ad["new_since"] + ad["old_since"]) < 10:
                return "pending", ad
            if ad["new_since"] == 0 and since_days >= 28:
                return "retire", {"why": "no adoption in 4 weeks", **ad}
            return "keep", ad
        if t == "agent_type":
            n = [i for i in corpus.invocations if i.agent_type == case["name"]
                 and i.started and i.started >= at]
            if not n and since_days >= 28:
                return "retire", {"why": "no invocations since adoption", "invocations": 0}
            done = sum(i.status == "completed" for i in n)
            return ("keep" if n else "pending"), {"invocations": len(n),
                                                 "completed_rate": round(done / len(n), 3) if n else None}
        if t == "route":
            key = case["key"].split("|")
            if key[0] in ("drift", "unapproved") and len(key) == 3:
                evs = [e for e in corpus.events if e.kind == "model" and e.surface == key[1]
                       and e.model == key[2] and e.ts >= at]
                return ("review" if evs else "keep"), {"calls_since": len(evs),
                                                        "why": "the pair still runs" if evs else "stopped"}
    return "pending", {"why": "no measurable case recorded for this artifact"}
