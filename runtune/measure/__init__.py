"""Did the artifact change anything? Measured, with the traps named.

Every measurement here is OBSERVATIONAL: rates over traffic that happened to
run, split at the date an artifact went live. Three things make that honest
enough to act on, each learned from a result that was nearly published wrong:

  A CONTROL. The target's change is reported next to the change in comparable
  traffic the artifact could not have touched (the same surface, not matching).
  The net effect is the difference of the two (difference-in-differences). In
  the guard A/B this system came from, the guarded arm's spread tightened — and
  both controls tightened the same way. Without controls that was the headline.

  THE ATTEMPT SHIFT. A rate can rise because behaviour improved: a nudge that
  makes agents stop reaching for `psql` removes the easy cases and leaves the
  hard ones, so the failure rate went 7.6% -> 19.0% while attempts fell 980 -> 174.
  A report that shows only rates calls that win a loss. Attempts per active day,
  as a share of all activity, are reported next to every rate.

  COVERAGE GAPS. A window that contains a recorder outage compares data with
  no data. Gap days are excluded from both windows and the report says how many
  days each window actually had.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone


def _as_dt(d) -> datetime:
    if isinstance(d, datetime):
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(d)).replace(tzinfo=timezone.utc)


def _gap_days(corpus, source=None) -> dict[str, set[date]]:
    """Gap days PER SOURCE. A day one recorder missed is not a missing day for the
    others — pooling them once erased every Claude attempt that fell inside a
    61-day Codex outage, and the before/after read "0 attempts" on both sides."""
    out: dict[str, set[date]] = {}
    for src, cov in corpus.coverage.items():
        if source and src != source:
            continue
        days = out.setdefault(src, set())
        for a, b, _ in cov.gaps:
            d, end = date.fromisoformat(a), date.fromisoformat(b)
            while d <= end:
                days.add(d)
                d += timedelta(days=1)
    return out


def _window(events, lo, hi, gaps):
    return [e for e in events if lo <= e.ts < hi and e.ts.date() not in gaps.get(e.source, ())]


def _rate(evs):
    n = len(evs)
    f = sum(not e.ok for e in evs)
    return {"attempts": n, "fails": f, "fail_rate": round(f / n, 4) if n else None}


def before_after(corpus, match, at, days: int = 28, control=None, source: str | None = None) -> dict:
    """Split target traffic at `at`; compare with control traffic over the same windows.

    match    predicate(event) -> bool: the traffic the artifact is about
    control  predicate(event) -> bool: comparable traffic it cannot touch.
             Default: same surfaces as the target, not matching.
    """
    at = _as_dt(at)
    evs = [e for e in corpus.events if (source is None or e.source == source)]
    gaps = _gap_days(corpus, source)
    lo, hi = at - timedelta(days=days), at + timedelta(days=days)
    target = [e for e in evs if match(e)]
    surfaces = {e.surface for e in target}
    if control is None:
        control = lambda e: e.surface in surfaces and not match(e)  # noqa: E731
    ctrl = [e for e in evs if control(e)]
    everything = evs

    def side(lo_, hi_):
        t = _window(target, lo_, hi_, gaps)
        c = _window(ctrl, lo_, hi_, gaps)
        allw = _window(everything, lo_, hi_, gaps)
        active_days = len({e.ts.date() for e in allw}) or 1
        return {"target": _rate(t), "control": _rate(c),
                "active_days": active_days,
                "target_per_day": round(len(t) / active_days, 2),
                "target_share_of_all": round(len(t) / len(allw), 5) if allw else None,
                "sessions": len({e.session for e in t})}

    b, a = side(lo, at), side(at, hi)
    res = {"at": at.date().isoformat(), "window_days": days, "before": b, "after": a,
           "gap_days_excluded": {src: sorted(d.isoformat() for d in ds if lo.date() <= d < hi.date())
                                 for src, ds in gaps.items() if any(lo.date() <= d < hi.date() for d in ds)}}
    tb, ta = b["target"]["fail_rate"], a["target"]["fail_rate"]
    cb, ca = b["control"]["fail_rate"], a["control"]["fail_rate"]
    if None not in (tb, ta):
        res["target_change"] = round(ta - tb, 4)
    if None not in (tb, ta, cb, ca):
        res["control_change"] = round(ca - cb, 4)
        res["net_change"] = round((ta - tb) - (ca - cb), 4)
    if b["target_share_of_all"] and a["target_share_of_all"] is not None:
        res["attempt_share_change"] = round(a["target_share_of_all"] / b["target_share_of_all"] - 1, 3)
    res["verdict"] = _verdict(res)
    return res


def _verdict(r) -> str:
    b, a = r["before"]["target"]["attempts"], r["after"]["target"]["attempts"]
    if b < 5 or a < 5:
        return "insufficient: fewer than 5 attempts on one side"
    net = r.get("net_change")
    share = r.get("attempt_share_change")
    notes = []
    if share is not None and share <= -0.5:
        notes.append(f"agents stopped reaching for it (attempt share {share:+.0%}) — read the rate as the residual hard cases")
    if net is None:
        return "; ".join(notes) or "no control traffic"
    if abs(net) < 0.02:
        head = "no measurable effect beyond the control"
    elif net < 0:
        head = f"failure rate fell {abs(net):.1%} more than the control"
    else:
        head = f"failure rate ROSE {net:.1%} more than the control"
    return "; ".join([head] + notes)


def adoption(corpus, old, new, at=None, by: str = "week") -> dict:
    """Share of a need served by the new way, per period. `old`/`new` are predicates.

    Opportunity = old + new: the times the need came up at all. This is the
    capability-side analogue of autoharness's use-rate-against-opportunity, with
    the old way as the denominator's other half instead of a request counter.
    """
    per = defaultdict(Counter)
    for e in corpus.events:
        key = e.week if by == "week" else e.ts.date().isoformat()
        if new(e):
            per[key]["new"] += 1
        elif old(e):
            per[key]["old"] += 1
    rows = []
    for k in sorted(per):
        n, o = per[k]["new"], per[k]["old"]
        rows.append({"period": k, "old": o, "new": n, "share_new": round(n / (n + o), 3) if n + o else None})
    out = {"periods": rows}
    if at:
        at = _as_dt(at)
        after = [e for e in corpus.events if e.ts >= at]
        n = sum(1 for e in after if new(e))
        o = sum(1 for e in after if old(e) and not new(e))
        out["since"] = at.date().isoformat()
        out["share_new_since"] = round(n / (n + o), 3) if n + o else None
        out["new_since"], out["old_since"] = n, o
    return out


def epochs(corpus, min_share: float = 0.6, source: str | None = None) -> list[dict]:
    """Weeks where the dominant model changed, per source.

    Evidence gathered under one model is a claim about that model. When the model
    underneath changes, every artifact derived before the change is a candidate
    for re-validation: in the guard A/B, three of thirteen rules could not be
    provoked at all after a model upgrade, one of which had fired 199 times.
    """
    per = defaultdict(lambda: defaultdict(Counter))
    for e in corpus.events:
        if e.kind != "tool" or not e.model or (source and e.source != source):
            continue
        per[e.source][e.week][_family(e.model)] += 1
    out = []
    for src, weeks in per.items():
        prev = None
        for wk in sorted(weeks):
            c = weeks[wk]
            model, n = c.most_common(1)[0]
            if n / sum(c.values()) < min_share:
                continue
            if prev and model != prev:
                out.append({"source": src, "week": wk, "from": prev, "to": model})
            prev = model
    return out


def _family(model: str) -> str:
    """claude-opus-5-5[1m] and claude-opus-5-5 are the same model for this purpose."""
    m = model.split("[")[0].lower()
    return m
