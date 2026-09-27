"""runtune — agents should get better from their runs.

    runtune scan      what the evidence covers, per source, and where it has holes
    runtune derive    runs -> proposed constraints, capabilities, subagents, routes
    runtune stage     accept a proposal for review (writes drafts + evidence)
    runtune apply     a named human applies a staged artifact
    runtune review    measure every active artifact: keep / review / probe / retire / revalidate
    runtune retire    remove an artifact (a constraint retirement is a boundary change)
    runtune measure   ad-hoc before/after with a control, or adoption of a new way over an old one
    runtune replay    replay an existing guard ruleset over history, both hosts
    runtune routes    the approved-vs-ran-vs-billed traffic table
    runtune agents    per-agent-type census and verified token ratios
    runtune ledger    verify the hash chain
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import time
from datetime import datetime, timezone

from . import measure, report, sources
from .derive import capabilities, constraints, routes, subagents
from .lifecycle import ledger, promoter, review
from .lifecycle.store import Workspace


def _common(p):
    p.add_argument("--source", default="claude,codex,openrouter",
                   help="comma list of claude,codex,openrouter (Postgres), default all")
    p.add_argument("--db", default="RUNTUNE_DB_URL", help="DSN or env var holding one (default $RUNTUNE_DB_URL)")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--routes", help="path to the model allowlist (routes.json shape)")
    p.add_argument("--jsonl", action="append", help="portable evidence file(s); repeatable")
    p.add_argument("--workspace", default=".runtune")
    p.add_argument("--cache", type=int, default=0, metavar="MIN",
                   help="reuse a loaded corpus for MIN minutes (stored in the workspace)")


def _corpus(a):
    srcs = [s for s in a.source.split(",") if s] if not a.jsonl or a.source != "claude,codex,openrouter" else []
    key = re.sub(r"[^a-z0-9]+", "-", f"{','.join(srcs)}-{a.days}-{a.routes}-{a.jsonl}".lower())[:120]
    path = os.path.join(a.workspace, f".corpus-{key}.pkl")
    if a.cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < a.cache * 60:
        with open(path, "rb") as f:
            return pickle.load(f)
    t = time.time()
    c = sources.load(srcs, a.db, a.days, a.routes, a.jsonl)
    print(f"runtune: loaded {len(c.events):,} attempts, {len(c.invocations):,} invocations "
          f"from {', '.join(c.sources()) or 'nothing'} in {time.time() - t:.1f}s", file=sys.stderr)
    if a.cache:
        os.makedirs(a.workspace, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(c, f)
    return c


def cmd_scan(a):
    c = _corpus(a)
    print("\n".join(report.coverage_md(c)))
    eps = measure.epochs(c)
    print("## Model epochs\n")
    if not eps:
        print("- none detected")
    for e in eps:
        print(f"- {e['source']} {e['week']}: {e['from']} → {e['to']}")
    return 0


def cmd_derive(a):
    c = _corpus(a)
    ws = Workspace(a.workspace)
    cands, held = [], []
    for fn in (lambda: constraints.derive(c), lambda: capabilities.derive_inline(c),
               lambda: capabilities.derive_procedures(c), lambda: subagents.derive(c, a.agents_dir),
               lambda: routes.derive(c)):
        got, wh = fn()
        cands += got
        held += wh
    run = {"run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
           "sources": c.sources(), "days": a.days,
           "coverage": {k: v.line() for k, v in c.coverage.items()},
           "epochs": measure.epochs(c),
           "candidates": [x.to_dict() for x in cands], "withheld": [x.to_dict() for x in held]}
    path = ws.save_run(run)
    md = report.derive_md(run, c)
    out = a.out or os.path.join(a.workspace, f"report-{run['run_id']}.md")
    with open(out, "w") as f:
        f.write(md)
    by = {}
    for x in cands:
        by[x.kind] = by.get(x.kind, 0) + 1
    print(f"proposed {len(cands)} ({', '.join(f'{k} {v}' for k, v in sorted(by.items())) or 'none'}); "
          f"withheld {len(held)} — nothing was applied")
    print(f"candidates: {path}\nreport:     {out}")
    if not cands:
        print("0 proposed. Read the withheld table before concluding there was nothing to learn.")
    return 0


def cmd_stage(a):
    ws = Workspace(a.workspace)
    art = promoter.stage(ws, a.id, a.target_root, a.revises, a.allow_withheld, a.reason)
    print(f"staged {art['id']} -> would write {art['target']}\n"
          f"drafts: {ws.root}/drafts/{art['id']}/  evidence: {ws.root}/{art['evidence']}")
    return 0


def cmd_apply(a):
    ws = Workspace(a.workspace)
    art = promoter.apply(ws, a.id, a.approve, a.action, a.reason, a.eval)
    print(f"applied {art['id']} -> {art['target']} (approved by {art['approver']})")
    return 0


def cmd_retire(a):
    ws = Workspace(a.workspace)
    art = promoter.retire(ws, a.id, a.approve, a.reason)
    print(f"retired {art['id']}")
    return 0


def cmd_review(a):
    ws = Workspace(a.workspace)
    rows = review.review(ws, _corpus(a), a.window)
    if not rows:
        print("no active artifacts")
    for r in rows:
        print(f"{r['verdict']:<11} {r['kind']:<11} {r['id']}  {r['title'][:70]}")
        if a.verbose:
            print(json.dumps(r["detail"], indent=1, default=str))
    return 0


def cmd_measure(a):
    c = _corpus(a)
    if a.old and a.new:
        o, n = re.compile(a.old), re.compile(a.new)
        res = measure.adoption(c, lambda e: e.kind == "tool" and bool(o.search(e.text)),
                               lambda e: e.kind == "tool" and bool(n.search(e.text)), at=a.at)
    else:
        rx = re.compile(a.match)
        surf = set(a.surface.split(",")) if a.surface else None
        m = lambda e: (surf is None or e.surface in surf) and bool(rx.search(e.text))  # noqa: E731
        res = measure.before_after(c, m, a.at, days=a.window)
    print(json.dumps(res, indent=1, default=str))
    return 0


def cmd_replay(a):
    with open(a.ruleset) as f:
        data = json.load(f)
    rules = data.get("rules", data) if isinstance(data, dict) else data
    c = _corpus(a)
    rows = constraints.replay_ruleset(rules, c)
    print(report.table(rows, ["id", "action", "matched", "matched_fail", "fail_rate", "tier_now",
                              "over_ceiling", "hosts"]))
    if a.measure:
        # Each rule measured at its own promotion date (meta.added) against all other
        # shell traffic over the same days.
        print("\n| rule | at | before | after | net vs control | attempt share | verdict |\n|---|---|---|---|---:|---:|---|")
        shell = lambda e: e.surface in constraints.SHELL_SURFACES  # noqa: E731
        for r in rules:
            pats = r.get("any") or ([r["pattern"]] if r.get("pattern") else [])
            added = (r.get("meta") or {}).get("added")
            if r.get("tool") not in ("Bash", None) or not pats or not added:
                continue
            rx = re.compile("|".join(f"(?:{p})" for p in pats))
            m = lambda e, rx=rx: shell(e) and bool(rx.search(e.text))  # noqa: E731
            res = measure.before_after(c, m, added, days=a.window, control=lambda e, m=m: shell(e) and not m(e))
            b, af = res["before"]["target"], res["after"]["target"]
            pct = lambda x: "—" if x is None else f"{x:.1%}"  # noqa: E731
            sgn = lambda x: "—" if x is None else f"{x * 100:+.1f}"  # noqa: E731
            share = res.get("attempt_share_change")
            print(f"| {r.get('id')} | {res['at']} | {pct(b['fail_rate'])} ({b['attempts']}) "
                  f"| {pct(af['fail_rate'])} ({af['attempts']}) | {sgn(res.get('net_change'))} "
                  f"| {'—' if share is None else f'{share:+.0%}'} | {res['verdict']} |")
    return 0


def cmd_routes(a):
    print(report.table(routes.traffic_table(_corpus(a)),
                       ["mode", "model", "status", "calls", "ok_rate", "usd", "usd_per_ok", "p50_ms"]))
    return 0


def cmd_agents(a):
    rows = subagents.profile(_corpus(a))
    for r in rows:
        r["census"] = ", ".join(f"{k} {v:.0%}" for k, v in list(r["census"].items())[:6])
    print(report.table(rows, ["source", "agent_type", "invocations", "sessions", "completed_rate",
                              "reread_per_output", "verified_token_rows", "census"]))
    return 0


def cmd_ledger(a):
    ok, msg = ledger.verify(Workspace(a.workspace).ledger_path)
    print(msg)
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="runtune", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "derive", "review", "measure", "replay", "routes", "agents"):
        p = sub.add_parser(name)
        _common(p)
        if name == "derive":
            p.add_argument("--agents-dir", help="agent definitions to check for over-grant (.claude/agents)")
            p.add_argument("--out", help="write the markdown report here")
        if name == "review":
            p.add_argument("--window", type=int, default=28)
            p.add_argument("-v", "--verbose", action="store_true")
        if name == "measure":
            p.add_argument("--at", required=True, help="date the change went live (YYYY-MM-DD)")
            p.add_argument("--match", help="regex over command/label text: the target traffic")
            p.add_argument("--surface", help="restrict to these surfaces (comma list)")
            p.add_argument("--window", type=int, default=28)
            p.add_argument("--old", help="adoption mode: regex for the old way")
            p.add_argument("--new", help="adoption mode: regex for the new way")
        if name == "replay":
            p.add_argument("--ruleset", required=True)
            p.add_argument("--measure", action="store_true",
                           help="also measure each rule at its meta.added date against a control")
            p.add_argument("--window", type=int, default=28)
    p = sub.add_parser("stage")
    p.add_argument("id")
    p.add_argument("--workspace", default=".runtune")
    p.add_argument("--target-root", default=".")
    p.add_argument("--revises")
    p.add_argument("--allow-withheld", action="store_true")
    p.add_argument("--reason")
    p = sub.add_parser("apply")
    p.add_argument("id")
    p.add_argument("--workspace", default=".runtune")
    p.add_argument("--approve", required=True, help="who is applying this (recorded in the ledger)")
    p.add_argument("--action", help="constraint action (monitor|nudge|deny|block), capped by tier")
    p.add_argument("--reason")
    p.add_argument("--eval", help="path to a passing eval result (required to widen a route)")
    p = sub.add_parser("retire")
    p.add_argument("id")
    p.add_argument("--workspace", default=".runtune")
    p.add_argument("--approve", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("ledger")
    p.add_argument("--workspace", default=".runtune")
    a = ap.parse_args(argv)
    if a.cmd == "measure" and not (a.match or (a.old and a.new)):
        ap.error("measure needs --match, or --old and --new")
    try:
        return globals()[f"cmd_{a.cmd}"](a)
    except promoter.Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
