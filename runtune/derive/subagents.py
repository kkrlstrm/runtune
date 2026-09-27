"""Isolated, repeated work -> a skilled subagent.

AutoRefine's third artifact type, grounded in what agents actually called rather
than in what a model imagines they need.

The observation this is built on (measured in the system RunTune came from):
a generic sub-agent carries the full tool-schema set and a listing of every
installed skill, and re-reads all of it every turn. Across 8,824 production
workflow sub-agents that was ~177 tokens re-read per token of output; narrow
purpose-built types doing the same work measured 23–59:1. The saving is in the
GRANT, not in the prompt — so the grant has to come from evidence.

  GRANT FROM THE CENSUS. A proposed agent type is granted exactly the tools its
  predecessors used, every one of them. A tool used by 2% of agents stays in the
  grant, marked as a fallback: a blanket narrow grant would have broken the 1,097
  agents that used WebSearch in one fan-out. Narrowing below the census is a human
  decision, never a derived one.

  COMPILE ORDER. A subagent is proposed only when a skill cannot close the work:
  the work is already being done in isolated contexts, many at a time, with a
  toolset narrower than the host's default. Otherwise the ladder stops at skill.

  OVER-GRANT on existing types (needs --agents-dir): a declared tool no
  invocation used in the window is a tightening candidate. Tightening is
  proposable; widening a grant always needs a human.

  CONTRACT LINT (from AutoRefine): an agent whose definition never states how it
  reports failure cannot be told apart from one that succeeded with nothing.

Token figures use only per-agent-verified rows (`token_verified`). Everything else
here is counts, which the historical token bug never touched.
"""

from __future__ import annotations

import os
import re
import statistics
from collections import Counter, defaultdict

from ..evidence import tiers
from .candidate import TIGHTEN, Candidate

GENERIC_TYPES = {"general-purpose", "workflow-subagent", "claude", "default", "worker"}
FANOUT_MIN = 5          # agents of one type in one session = a fan-out
FALLBACK_BELOW = 0.05   # a tool used by fewer than 5% of agents is a fallback grant
MAX_GRANT = 4           # above this many tools a narrow type saves little over the generic one

# What each recorder can see. cc-logger captures an allowlist of tools, so a grant
# of Grep or Glob can never show up as used — unobservable is not unused, and an
# over-grant finding on an invisible tool would narrow a grant on no evidence.
OBSERVABLE = {"claude": {"Agent", "Bash", "Edit", "Write", "Read", "Skill", "WebFetch", "WebSearch"}}


def observable(source: str, tool: str) -> bool:
    seen = OBSERVABLE.get(source)
    return seen is None or tool in seen or tool.startswith("mcp__")


def reread_ratio(invs) -> float | None:
    v = [i.tokens_reread / i.tokens_out for i in invs if i.token_verified and i.tokens_out > 0]
    return round(statistics.median(v), 1) if len(v) >= 5 else None


def census(invs) -> dict:
    n = len(invs)
    used = Counter()
    for i in invs:
        for t in i.tools:
            used[t] += 1
    return {t: round(c / n, 3) for t, c in used.most_common()} if n else {}


def profile(corpus) -> list[dict]:
    """Per agent type: volume, completion, census, verified token ratio."""
    by = defaultdict(list)
    for i in corpus.invocations:
        if i.agent_type and i.agent_type != "root":
            by[(i.source, i.agent_type)].append(i)
    out = []
    for (src, t), invs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        done = sum(1 for i in invs if i.status == "completed")
        out.append({"source": src, "agent_type": t, "invocations": len(invs),
                    "completed_rate": round(done / len(invs), 3),
                    "sessions": len({i.session for i in invs}),
                    "census": census(invs), "reread_per_output": reread_ratio(invs),
                    "verified_token_rows": sum(i.token_verified for i in invs)})
    return out


def load_agent_defs(agents_dir: str | None) -> dict:
    """Parse `.claude/agents/*.md` frontmatter: name -> {tools, body}."""
    defs = {}
    if not agents_dir or not os.path.isdir(agents_dir):
        return defs
    for fn in sorted(os.listdir(agents_dir)):
        if not fn.endswith(".md"):
            continue
        text = open(os.path.join(agents_dir, fn)).read()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        fm, body = m.group(1), m.group(2)
        name = re.search(r"^name:\s*(.+)$", fm, re.M)
        tools = re.search(r"^tools:\s*(.+)$", fm, re.M)
        defs[(name.group(1) if name else fn[:-3]).strip()] = {
            "tools": [t.strip() for t in tools.group(1).split(",")] if tools else ["*"],
            "body": body, "path": os.path.join(agents_dir, fn)}
    return defs


def derive(corpus, agents_dir: str | None = None) -> tuple[list, list]:
    profiles = {(p["source"], p["agent_type"]): p for p in profile(corpus)}
    narrow_ratios = [p["reread_per_output"] for p in profiles.values()
                     if p["agent_type"] not in GENERIC_TYPES and p["reread_per_output"]]
    narrow_ref = round(statistics.median(narrow_ratios), 1) if narrow_ratios else None
    out, withheld = [], []

    # --- 1. generic fan-outs whose census is narrow -> propose a purpose-built type
    fanouts = defaultdict(list)
    for i in corpus.invocations:
        if i.agent_type in GENERIC_TYPES:
            fanouts[(i.source, i.agent_type, i.session)].append(i)
    # Fold each fan-out into the smallest observed toolset that contains its own:
    # {Bash} and {Bash, WebFetch} fan-outs are the same worker on a lighter day.
    sized = []
    for (src, t, _sess), invs in fanouts.items():
        if len(invs) >= FANOUT_MIN:
            ts = frozenset(census(invs))
            if ts:
                sized.append((src, t, ts, invs))
    supersets = sorted({(src, t, ts) for src, t, ts, _ in sized if len(ts) <= MAX_GRANT},
                       key=lambda x: -len(x[2]))
    by_toolset = defaultdict(list)
    for src, t, ts, invs in sized:
        home = [s_ for s_ in supersets if s_[0] == src and s_[1] == t and ts <= s_[2]]
        home = min(home, key=lambda x: len(x[2])) if home else (src, t, ts)
        by_toolset[(home[0], home[1], tuple(sorted(home[2])))].append(invs)
    # keep only maximal sets: a set contained in another set that has groups is merged into it
    for key in sorted(list(by_toolset), key=lambda k: len(k[2])):
        bigger = [k for k in by_toolset if k != key and k[:2] == key[:2] and set(key[2]) < set(k[2])
                  and len(k[2]) <= MAX_GRANT]
        if bigger:
            by_toolset[min(bigger, key=lambda k: len(k[2]))].extend(by_toolset.pop(key))
    for (src, t, toolset), groups in sorted(by_toolset.items(), key=lambda kv: -sum(map(len, kv[1]))):
        agents = [i for g in groups for i in g]
        cen = census(agents)
        weeks = len({i.started.isocalendar()[:2] for i in agents if i.started})
        reuse = tiers.classify_reuse(len(groups), weeks)
        generic = profiles.get((src, t), {})
        fallbacks = [k for k, v in cen.items() if v < FALLBACK_BELOW]
        name = "-".join(x.lower().replace("mcp__", "") for x in toolset)[:40] + "-worker"
        c = Candidate(
            kind="subagent", key=f"{src}|{t}|{'+'.join(toolset)}",
            title=f"{len(groups)} `{t}` fan-outs only ever used {{{', '.join(toolset)}}} — give them a purpose-built type",
            claim=(f"{len(agents):,} {t} agents across {len(groups)} fan-outs used only "
                   f"{len(toolset)} tool(s); the generic type grants everything and lists every skill."),
            tier=reuse, direction=TIGHTEN, sources=[src],
            numbers={"fanouts": len(groups), "agents": len(agents), "weeks": weeks, "census": cen,
                     "fallback_tools": fallbacks,
                     "completed_rate": round(sum(i.status == "completed" for i in agents) / len(agents), 3),
                     "generic_reread_per_output": generic.get("reread_per_output"),
                     "narrow_types_reread_per_output_median": narrow_ref},
            ladder=[("constraint", False, "nothing is failing; the cost is context carried per agent"),
                    ("skill", False, f"the work already runs as {len(agents):,} isolated agents, "
                                     f"~{round(len(agents) / len(groups))} per fan-out — in the parent's "
                                     "context it would multiply, not shrink"),
                    ("subagent", True, "isolated, repeated, and its toolset is measured")],
            requires=["human-review"],
            proposal={"agent_md": _agent_md(name, toolset, fallbacks, len(agents), len(groups)),
                      "grant": list(toolset), "grant_basis": "census — every tool any agent used"},
        )
        c.gate("breadth", reuse != tiers.LOCAL, f"{len(groups)} fan-outs over {weeks} weeks")
        c.gate("narrower-than-host", len(toolset) <= MAX_GRANT,
               f"census has {len(toolset)} tools; a grant this wide saves little")
        g_ratio = generic.get("reread_per_output")
        if g_ratio and narrow_ref and g_ratio <= narrow_ref:
            c.caveats.append(f"NO token saving expected: generic {t} already re-reads {g_ratio}:1, at or below "
                             f"the narrow types' {narrow_ref}:1 — the case for this type is the grant "
                             "(least privilege), not cost")
        elif g_ratio and narrow_ref:
            c.caveats.append(f"projection, not a measurement: generic {t} re-reads "
                             f"{generic['reread_per_output']}:1, existing narrow types {narrow_ref}:1 (medians, "
                             "verified-token rows only)")
        c.caveats.append("fan-outs are grouped by toolset, not by task: read a sample before naming the type")
        (out if c.admitted else withheld).append(c)

    # --- 2. existing narrow types: over-grant and contract lint
    defs = load_agent_defs(agents_dir)
    for name, d in defs.items():
        prof = next((p for (s, t), p in profiles.items() if t == name), None)
        granted = [g for g in d["tools"] if g != "*"]
        if prof and granted and prof["invocations"] >= 10:
            unused = [g for g in granted if g not in prof["census"] and observable(prof["source"], g)]
            invisible = [g for g in granted if not observable(prof["source"], g)]
            if unused:
                c = Candidate(
                    kind="subagent", key=f"overgrant|{name}",
                    title=f"`{name}` is granted {', '.join(unused)} and never used it",
                    claim=f"{prof['invocations']} invocations; declared tools never called: {', '.join(unused)}.",
                    tier=tiers.classify_reuse(prof["sessions"], 2), direction=TIGHTEN, sources=[prof["source"]],
                    numbers={"invocations": prof["invocations"], "granted": granted, "census": prof["census"]},
                    ladder=[("subagent", True, "revision of an existing type — lineage declared, not inferred")],
                    requires=["human-review"],
                    proposal={"revises": name, "grant": [g for g in granted if g not in unused]},
                    caveats=["unused in the window is not unneeded forever — keep a tool the agent's "
                             "instructions name as a fallback"]
                    + ([f"not judged (the recorder cannot see them): {', '.join(invisible)}"] if invisible else []))
                c.gate("volume", prof["invocations"] >= 10, f"{prof['invocations']} invocations")
                out.append(c)
        if not re.search(r"\b(status|fail(ed|ure)?|error|not[ _-]?found|blocked)\b", d["body"], re.I):
            c = Candidate(
                kind="subagent", key=f"contract|{name}", title=f"`{name}` never says how it reports failure",
                claim="the definition declares no termination status, so an empty success and a failure read the same.",
                tier="lint", sources=["agents-dir"], requires=["human-review"],
                ladder=[("subagent", True, "contract fix on an existing type")],
                proposal={"revises": name, "add": "a required status field (ok | not_found | failed) in the output"})
            c.gate("lint", True, "AutoRefine: a subagent must return a status that separates success from failure")
            out.append(c)

    return out, withheld


def _agent_md(name, toolset, fallbacks, n_agents, n_fanouts) -> str:
    tools = ", ".join(toolset)
    lines = ["---", f"name: {name}",
             "description: DRAFT — name this for the task these fan-outs perform, then describe when to use it",
             f"tools: {tools}", "---", "",
             f"Purpose-built worker derived from {n_agents:,} generic sub-agents across {n_fanouts} fan-outs,",
             f"which between them used only: {tools}.", ""]
    if fallbacks:
        lines.append(f"Fallback tools (rarely used, kept so the census is not broken): {', '.join(fallbacks)}.")
        lines.append("")
    lines += ["Return a result with a required `status` field: `ok`, `not_found`, or `failed`, "
              "plus the reason when it is not `ok`."]
    return "\n".join(lines) + "\n"
