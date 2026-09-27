"""Model routing: what was approved, what ran, what was billed.

An allowlist is a statement of intent. Nothing about writing it down makes a
call site honour it — the system RunTune came from had a written six-mode policy,
each mode cleared by an eval, and a model on no list ran 1,464 production
requests and classified 34,162 rows before anyone asked "what actually ran?".
When it was finally scored it was worst of five. That question is this module.

Findings, by which side of the loop they land on:

  CONSTRAINTS (tighten — "don't do this again")
    drift         a mode ran on a model other than the one its eval cleared
    unapproved    traffic under a mode the policy does not contain, or none at all
    unreliable    a (mode, model) pair whose requests fail at a tier rate
    unattributed  models on the bill that never appear in the calls log — traffic
                  that bypassed the router, so no mode, no caller, no policy

  LIFECYCLE (the allowlist's own hygiene)
    idle          an approved mode with no traffic in the window: a clearance
                  nothing uses is a clearance nobody re-checks
    stale         an approved mode whose verification is older than its policy's
                  staleness window

  CAPABILITIES (widen — "do this again, cheaper")
    challenger    a model that served the same mode at >= the approved model's
                  reliability for less per successful call. ALWAYS "eval required":
                  `ok` is transport success, not answer quality, and a model can be
                  100% ok and worst of five. The allowlist moves on an eval, never
                  on this table.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

from ..evidence import tiers
from .candidate import NEUTRAL, TIGHTEN, WIDEN, Candidate

MIN_CALLS = 20
# Callers whose traffic is an experiment, not production. Their failures and their
# off-policy models are what a benchmark is FOR; counting them as drift reported a
# bench's 32% failure rate as a production route failing.
BENCH_CALLER = re.compile(r"(?i)bench|eval|experiment|probe|regression|test_|_test|scaffold|bakeoff")


def _model_eq(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.split(":")[0].lower() == b.split(":")[0].lower()


def pair_stats(events) -> dict:
    n = len(events)
    ok = sum(e.ok for e in events)
    usd = sum(e.usd for e in events)
    lat = sorted(e.duration_ms for e in events if e.duration_ms)
    return {"calls": n, "ok": ok, "ok_rate": round(ok / n, 4) if n else None,
            "usd": round(usd, 4), "usd_per_ok": round(usd / ok, 6) if ok else None,
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "callers": dict(Counter(e.actor for e in events).most_common(5)),
            "first": min(e.ts for e in events).isoformat() if n else None,
            "last": max(e.ts for e in events).isoformat() if n else None}


def derive(corpus, today: date | None = None) -> tuple[list, list]:
    today = today or datetime.now(timezone.utc).date()
    calls = [e for e in corpus.events if e.kind == "model"]
    policy = corpus.route_policy or {"modes": {}, "retired": {}, "staleness_warn_days": 120}
    modes = policy["modes"]
    # A call made before its mode's current clearance date ran under an EARLIER
    # policy. Judging it against today's allowlist reports a re-clearance as drift.
    # A clearance with only a DATE cannot say what hour it took effect, so its own day
    # is a transition day: off-policy calls on it are not evidence of drift. (The dial
    # modes swapped models at ~11:00 UTC on their clearance day; every "drift" call
    # the first version reported came from that morning.)
    pairs = defaultdict(list)
    pre_clearance = Counter()
    bench = Counter()
    for e in calls:
        if BENCH_CALLER.search(e.actor or "") or BENCH_CALLER.search(e.session or ""):
            bench[(e.surface, e.model)] += 1
            continue
        vd = (modes.get(e.surface) or {}).get("verified_date")
        if vd and e.ts.date() <= date.fromisoformat(vd) and not _model_eq(e.model, modes[e.surface].get("model")):
            pre_clearance[(e.surface, e.model)] += 1
            continue
        pairs[(e.surface, e.model)].append(e)
    out, withheld = [], []

    # ---- constraints: drift / unapproved / unreliable
    for (mode, model), evs in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        st = pair_stats(evs)
        approved = modes.get(mode)
        if not approved:
            retired = mode in policy.get("retired", {})
            c = Candidate(
                kind="route", key=f"unapproved|{mode}|{model}",
                title=f"`{model}` ran {st['calls']:,} requests under {'retired ' if retired else ''}mode `{mode}`, "
                      "which the allowlist does not clear",
                claim=f"{st['calls']:,} requests, ${st['usd']:.2f}, from {len(st['callers'])} caller(s); "
                      f"no eval cleared this mode.",
                tier=tiers.RECURRING if st["calls"] >= MIN_CALLS else tiers.LOCAL, direction=TIGHTEN,
                sources=["openrouter"], numbers=st, requires=["human-review"],
                ladder=[("constraint", True, "a check at the router's single chokepoint closes it")],
                proposal={"route_check": {"mode": mode, "model": model, "action": "refuse-unless-cleared"}})
            c.gate("volume", st["calls"] >= MIN_CALLS, f"{st['calls']} calls")
            (out if c.admitted else withheld).append(c)
            continue
        if not _model_eq(model, approved.get("model")):
            c = Candidate(
                kind="route", key=f"drift|{mode}|{model}",
                title=f"mode `{mode}` is cleared for `{approved['model']}` but {st['calls']:,} requests ran on `{model}`",
                claim=f"{st['calls']:,} requests (${st['usd']:.2f}) from {', '.join(st['callers'])} "
                      f"used a model the mode's eval never scored.",
                tier=tiers.RECURRING if st["calls"] >= MIN_CALLS else tiers.LOCAL, direction=TIGHTEN,
                sources=["openrouter"], numbers={**st, "approved_model": approved["model"]},
                requires=["human-review"],
                ladder=[("constraint", True, "the router can refuse a mode/model pair its policy does not list")],
                proposal={"route_check": {"mode": mode, "model": model, "expected": approved["model"],
                                          "action": "refuse-or-reroute"}},
                caveats=["may be a deliberate fallback chain; if so, the policy should say so"])
            c.gate("volume", st["calls"] >= MIN_CALLS, f"{st['calls']} calls")
            (out if c.admitted else withheld).append(c)
        tier, rate = tiers.classify_failure(st["calls"] - st["ok"], st["calls"])
        if tier in (tiers.DETERMINISTIC, tiers.REPRODUCIBLE) or (rate and rate >= 0.10 and st["calls"] >= 50):
            c = Candidate(
                kind="route", key=f"unreliable|{mode}|{model}",
                title=f"`{model}` fails {rate:.0%} of `{mode}` requests",
                claim=f"{st['calls'] - st['ok']} of {st['calls']} requests did not return ok.",
                tier=tier, direction=TIGHTEN, sources=["openrouter"], numbers=st,
                requires=["human-review"],
                ladder=[("constraint", True, "reroute or retry policy at the router")],
                caveats=["transport failures only; says nothing about answer quality"])
            c.gate("denominator", tier != tiers.ANECDOTAL, f"{st['calls']} calls")
            out.append(c)

    # ---- reconciliation: bill vs calls log
    billed = {}
    logged = Counter()
    if corpus.spend:
        logged = Counter()
        for e in calls:
            logged[(e.ts.date().isoformat(), (e.model or "").lower())] += 1
        call_days = {e.ts.date().isoformat() for e in calls}
        billed = defaultdict(lambda: {"requests": 0, "usd": 0.0, "days": set()})
        for r in corpus.spend:
            d = str(r.get("usage_date"))
            if d not in call_days:
                continue  # only compare days both records cover
            k = (r.get("model") or "").lower()
            billed[k]["requests"] += int(r.get("requests") or 0)
            billed[k]["usd"] += float(r.get("usage_usd") or 0)
            billed[k]["days"].add(d)
    def unlogged(model):
        b = billed.get((model or "").lower())
        if not b:
            return 0
        return b["requests"] - sum(n for (d, m), n in logged.items() if m == (model or "").lower() and d in b["days"])

    # ---- lifecycle: idle and stale clearances, one finding per mode
    used_modes = {m for (m, _) in pairs} | {m for (m, _) in pre_clearance}
    stale_days = policy.get("staleness_warn_days", 120)
    for mode, spec in modes.items():
        vd = spec.get("verified_date")
        age = (today - date.fromisoformat(vd)).days if vd else None
        idle = mode not in used_modes and bool(calls)
        stale = age is not None and age > stale_days
        if not (idle or stale):
            continue
        bypass = unlogged(spec.get("model")) if idle else 0
        if idle and bypass >= MIN_CALLS:
            # The router saw nothing, the bill saw plenty: the mode is in use by a caller
            # that bypasses the chokepoint. Retiring it would break that caller.
            c = Candidate(
                kind="route", key=f"bypass|{mode}",
                title=f"approved mode `{mode}` looks idle to the router, but its model was billed "
                      f"{bypass:,} requests outside it",
                claim="the clearance is in use by a caller that bypasses the router — route that caller "
                      "through the chokepoint; do NOT retire the mode on the router's evidence alone.",
                tier=tiers.RECURRING, direction=TIGHTEN, sources=["openrouter"],
                numbers={"approved_model": spec.get("model"), "billed_outside_router": bypass,
                         "verified_date": vd, "age_days": age, "stale": stale},
                requires=["human-review"] + (["eval"] if stale else []),
                ladder=[("constraint", True, "route the bypassing caller through the one chokepoint")],
                caveats=["the calls log and the bill disagree; the bill is the one that cannot be bypassed"])
            c.gate("cross-check", True, "idle verdict overturned by billing data")
            out.append(c)
            continue
        what = " and ".join(x for x, on in (("idle in the window", idle),
                                            (f"last verified {age} days ago (warns at {stale_days})", stale)) if on)
        c = Candidate(
            kind="route", key=f"lifecycle|{mode}",
            title=f"approved mode `{mode}` is {what}",
            claim=("a clearance nothing uses is a clearance nobody re-checks — retire it, or re-run its eval "
                   "before routing work to it" if idle else
                   "the model behind this mode has had time to change underneath its eval — re-run its regression spec"),
            tier="lifecycle", direction=TIGHTEN if idle else NEUTRAL, sources=["openrouter"],
            numbers={"approved_model": spec.get("model"), "verified_date": vd, "age_days": age,
                     "idle": idle, "stale": stale,
                     "traffic": len(pairs.get((mode, spec.get("model")), []))},
            requires=["human-review"] + (["eval"] if stale and not idle else []),
            proposal={"retire_mode": mode} if idle else {"revalidate_mode": mode},
            ladder=[("constraint", True, "retiring a clearance narrows what may run")],
            caveats=["the calls log covers only the router; a mode used by a caller that bypasses it "
                     "reads as idle here"] if idle else [])
        c.gate("window", True, "")
        out.append(c)

    # ---- capabilities: challengers inside a mode
    by_mode = defaultdict(dict)
    for (mode, model), evs in pairs.items():
        by_mode[mode][model] = pair_stats(evs)
    for mode, models in by_mode.items():
        approved = modes.get(mode, {}).get("model")
        base = next((s for m, s in models.items() if _model_eq(m, approved)), None)
        if not base or not base["usd_per_ok"] or base["calls"] < MIN_CALLS:
            continue
        for model, st in models.items():
            if _model_eq(model, approved) or st["calls"] < MIN_CALLS or not st["usd_per_ok"]:
                continue
            if st["ok_rate"] >= base["ok_rate"] and st["usd_per_ok"] < base["usd_per_ok"]:
                c = Candidate(
                    kind="route", key=f"challenger|{mode}|{model}",
                    title=f"`{model}` served `{mode}` at {st['ok_rate']:.1%} ok for "
                          f"{st['usd_per_ok'] / base['usd_per_ok']:.0%} of the approved model's cost per ok call",
                    claim="cheaper at equal-or-better reliability on this mode's real traffic — a reason to run "
                          "the mode's eval against it, not a reason to route to it.",
                    tier=tiers.RECURRING, direction=WIDEN, sources=["openrouter"],
                    numbers={"challenger": st, "approved": base, "approved_model": approved},
                    requires=["eval", "human-review"],
                    ladder=[("skill", True, "a routing capability: same job, cheaper model")],
                    proposal={"eval_request": {"mode": mode, "candidate": model, "incumbent": approved}},
                    caveats=["ok is transport success, not quality", "traffic may differ between the two models "
                             "(different callers, different inputs) — this is not a matched comparison"])
                c.gate("eval-before-route", True, "widening an allowlist requires a passing eval; RunTune will "
                                                  "not apply this without one")
                out.append(c)

    if corpus.spend:
        rows = []
        for model, b in sorted(billed.items(), key=lambda kv: -kv[1]["requests"]):
            seen = sum(n for (d, m), n in logged.items() if m == model and d in b["days"])
            gap = b["requests"] - seen
            if b["requests"] >= MIN_CALLS and gap / b["requests"] >= 0.5:
                rows.append({"model": model, "billed": b["requests"], "logged": seen, "usd": round(b["usd"], 2),
                             "on_allowlist": any(_model_eq(model, s.get("model")) for s in modes.values())})
        if rows:
            total = sum(r["billed"] - r["logged"] for r in rows)
            usd = sum(r["usd"] for r in rows)
            off = [r for r in rows if not r["on_allowlist"]]
            c = Candidate(
                kind="route", key="unattributed",
                title=f"{total:,} billed requests on {len(rows)} model{'s' if len(rows) != 1 else ''} never passed "
                      f"through the router ({len(off)} of them on no allowlist)",
                claim=f"${usd:.2f} billed on days the calls log was running, with no mode, caller or policy check "
                      "recorded. This is the traffic an allowlist cannot see.",
                tier=tiers.RECURRING, direction=TIGHTEN, sources=["openrouter"],
                numbers={"models": rows, "unlogged_requests": total, "usd": round(usd, 2)},
                requires=["human-review"],
                ladder=[("constraint", True, "route every caller through the one chokepoint, or label it")],
                caveats=["benchmark and eval scripts that call the API directly land here by design — label "
                         "them rather than counting them as production drift"])
            c.gate("volume", True, "")
            out.append(c)
    if pre_clearance:
        for (mode, model), n in pre_clearance.most_common():
            if n >= MIN_CALLS:
                withheld_c = Candidate(
                    kind="route", key=f"preclearance|{mode}|{model}",
                    title=f"`{mode}` ran {n:,} requests on `{model}` before its current clearance date",
                    claim="judged against the policy in force at the time, not today's; not drift.",
                    tier="history", sources=["openrouter"], numbers={"calls": n})
                withheld_c.gate("current-policy", False, "these calls predate the mode's clearance, "
                                                         "or fall on its (hour-less) clearance day")
                withheld.append(withheld_c)
    for (mode, model), n in bench.most_common():
        if n >= MIN_CALLS:
            b = Candidate(kind="route", key=f"bench|{mode}|{model}",
                          title=f"`{mode}` on `{model}`: {n:,} requests from benchmark/eval callers",
                          claim="experiment traffic; excluded from drift and reliability findings.",
                          tier="history", sources=["openrouter"], numbers={"calls": n})
            b.gate("production-traffic", False, "caller is a benchmark or eval")
            withheld.append(b)
    return out, withheld


def traffic_table(corpus) -> list[dict]:
    """Every (mode, model) pair with its approval status — the ep04 table."""
    modes = (corpus.route_policy or {}).get("modes", {})
    pairs = defaultdict(list)
    for e in corpus.events:
        if e.kind == "model":
            pairs[(e.surface, e.model)].append(e)
    rows = []
    for (mode, model), evs in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        st = pair_stats(evs)
        ap = modes.get(mode, {}).get("model")
        status = "approved" if _model_eq(model, ap) else ("drift" if ap else "unapproved")
        rows.append({"mode": mode, "model": model, "status": status, **{k: st[k] for k in
                     ("calls", "ok_rate", "usd", "usd_per_ok", "p50_ms")}})
    for mode, spec in modes.items():
        if not any(r["mode"] == mode for r in rows):
            rows.append({"mode": mode, "model": spec.get("model"), "status": "approved-idle", "calls": 0,
                         "ok_rate": None, "usd": 0, "usd_per_ok": None, "p50_ms": None})
    return rows
