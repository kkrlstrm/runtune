"""Markdown rendering. Every report states what the evidence could not see first."""

from __future__ import annotations

import json

KIND_HEAD = {
    "constraint": "Constraints — failure / drift → \"don't do this again\"",
    "capability": "Capabilities — success / reuse → \"do this again\"",
    "subagent": "Skilled subagents — isolated, repeated work → a purpose-built type",
    "route": "Routes — what was approved vs. what ran vs. what was billed",
}


def coverage_md(corpus) -> list[str]:
    lines = ["## What the evidence covers", ""]
    for src in sorted(corpus.coverage):
        cov = corpus.coverage[src]
        lines.append(f"- {cov.line()}")
        for n in cov.notes:
            lines.append(f"  - note: {n}")
    if corpus.route_policy:
        lines.append(f"- route policy: {len(corpus.route_policy['modes'])} approved modes "
                     f"({corpus.route_policy.get('path', 'inline')})")
    lines.append("")
    return lines


def derive_md(run: dict, corpus) -> str:
    lines = [f"# RunTune derive — {run['run_id']}", ""]
    lines += coverage_md(corpus)
    cands, withheld = run["candidates"], run["withheld"]
    lines += ["## Funnel", "", "| kind | proposed | withheld |", "|---|---:|---:|"]
    for k in KIND_HEAD:
        lines.append(f"| {k} | {sum(c['kind'] == k for c in cands)} | {sum(c['kind'] == k for c in withheld)} |")
    lines.append("")
    if run.get("epochs"):
        lines += ["## Model epochs (evidence before a change may need re-validation)", ""]
        for e in run["epochs"]:
            lines.append(f"- {e['source']} {e['week']}: `{e['from']}` → `{e['to']}`")
        lines.append("")
    for k, head in KIND_HEAD.items():
        ks = [c for c in cands if c["kind"] == k]
        if not ks:
            continue
        lines += [f"## {head}", ""]
        for c in ks:
            lines += _cand_md(c)
    wh = [c for c in withheld]
    if wh:
        lines += ["## Withheld (considered, not proposed — and why)", "", "| kind | cluster | failed gate |", "|---|---|---|"]
        for c in wh[:60]:
            failed = "; ".join(f"{g['name']}: {g['reason']}" for g in c["gates"] if not g["passed"] and not g["suppressed"])
            lines.append(f"| {c['kind']} | {c['title'][:90].replace('|', '/')} | {failed[:140].replace('|', '/')} |")
        if len(wh) > 60:
            lines.append(f"| … | {len(wh) - 60} more | |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _cand_md(c) -> list[str]:
    out = [f"### `{c['id']}`", f"**{c['title']}**", "", c["claim"], "",
           f"tier `{c['tier']}` · direction `{c['direction']}` · sources {', '.join(c['sources'])} · "
           f"requires {', '.join(c['requires']) or '—'}", ""]
    ladder = " → ".join(f"{r[0]} {'✓' if r[1] else '✗'}" for r in c.get("ladder", []))
    if ladder:
        out += [f"ladder: {ladder}", ""]
    for g in c.get("gates", []):
        out.append(f"- gate {'✓' if g['passed'] else '✗'} {g['name']}: {g['reason']}")
    for cv in c.get("caveats", []):
        out.append(f"- caveat: {cv}")
    nums = {k: v for k, v in c.get("numbers", {}).items() if k not in ("census",)}
    out += ["", "<details><summary>numbers</summary>", "", "```json",
            json.dumps(nums, indent=1, default=str)[:1500], "```", "</details>", ""]
    return out


def table(rows: list[dict], cols: list[str]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    return "\n".join(out)
