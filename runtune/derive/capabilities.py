"""SUCCESS / REUSE -> capability.  "Do this again."

The failure side asks "what keeps breaking". The success side asks a question
nothing in the recorder answers directly: **what do agents keep re-deriving?**
Work that succeeds every time is invisible to a failure counter, and it can
still be the most expensive thing in the log — because it is re-written from
scratch each time.

Two derivations, both deterministic:

  INLINE PROGRAMS. An agent that writes `python3 -c "from toolkit import db; …"` is
  writing a program to call a library that has no command-line entry point. The
  snippet works; the cost is that it is authored again every time, and averages
  hundreds of characters where a CLI call is a line. Snippets are clustered by
  WHAT LOCAL CODE THEY REACH (evidence.shapes.inline_target), not by their text,
  and the attribute calls inside them (`db.query_master(`, `crm.accounts_for(`)
  are ranked into a proposed verb list with its coverage curve. If a CLI for that
  library already exists in the log, the candidate is an ADOPTION gap instead:
  the capability exists and agents are not using it.

  PROCEDURES. Ordered triples of command shapes that recur, all succeeding,
  across many independent sessions. This is a frequency counter, and it is said
  so in every candidate: it finds recurring sequences, it does not understand
  them. A human (or a drafting model, under the promoter's gates) writes the
  skill; RunTune guarantees it is about something that actually recurs.

Admission is on BREADTH (sessions and weeks, evidence.tiers.classify_reuse), not
volume — one session re-running a snippet 300 times is one need, not 300.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..evidence import shapes, tiers
from .candidate import NEUTRAL, Candidate

MIN_EVENTS = 20
MIN_SUCCESS = 0.70
# Surfaces too generic to name a procedure after: every session reads and edits files.
_GENERIC = {"Read", "Edit", "Write", "Grep", "Glob", "apply_patch", "write_stdin", "wait",
            "update_plan", "TodoWrite"}
_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\s*\(")


def _weeks(events) -> int:
    return len({e.week for e in events})


def _cli_names(e) -> set[str]:
    """Names a shell call could be invoking a CLI by: `toolkit`, `toolkit.py`, `python3 toolkit.py`."""
    if not e.shape or e.inline_target:
        return set()
    return {t[:-3] if t.endswith(".py") else t for t in e.shape.split(" ")}


def derive_inline(corpus, min_events: int = MIN_EVENTS) -> tuple[list, list]:
    tool = [e for e in corpus.events if e.kind == "tool"]
    groups = defaultdict(list)
    for e in tool:
        if e.inline_target:
            groups[e.inline_target.split(":")[0]].append(e)

    out, withheld = [], []
    for root, evs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(evs) < min_events:
            continue
        sessions = {e.session for e in evs}
        weeks = _weeks(evs)
        reuse = tiers.classify_reuse(len(sessions), weeks)
        ok_rate = sum(e.ok for e in evs) / len(evs)
        chars = [e.chars for e in evs if e.chars]
        mean_chars = round(sum(chars) / len(chars)) if chars else None
        subs = Counter()
        for e in evs:
            for s in (e.inline_target.split(":", 1)[1].split(",") if ":" in e.inline_target else []):
                if s:
                    subs[s] += 1
        verbs = Counter()
        for e in evs:
            for mod, fn in _CALL.findall(e.text):
                if mod in subs or mod == root:
                    verbs[f"{mod}.{fn}"] += 1
        coverage = _coverage_curve(verbs)
        cli_events = [e for e in tool if root in _cli_names(e)]
        exists = bool(cli_events)
        observed_cli = _observed_invocations(root, cli_events)
        hosts = Counter(e.source for e in evs)

        c = Candidate(
            kind="capability", key=f"inline|{root}",
            title=(f"adoption gap: agents still hand-write `{root}` snippets although a CLI exists"
                   if exists else f"`{root}` is re-derived inline — promote it to a CLI + skill"),
            claim=(f"{len(evs):,} inline programs across {len(sessions)} sessions and {weeks} weeks "
                   f"exist only to reach `{root}`"
                   + (f", averaging {mean_chars} characters each" if mean_chars else "") + "."),
            tier=reuse, direction=NEUTRAL, sources=sorted(hosts),
            numbers={"events": len(evs), "sessions": len(sessions), "weeks": weeks,
                     "ok_rate": round(ok_rate, 3), "mean_chars": mean_chars,
                     "submodules": dict(subs.most_common(10)),
                     "verbs_top": verbs.most_common(15), "verbs_distinct": len(verbs),
                     "verbs_for_80pct": coverage.get(0.8), "verbs_for_95pct": coverage.get(0.95),
                     "cli_exists": exists, "cli_calls": len(cli_events), "cli_observed": observed_cli[:12],
                     "hosts": dict(hosts)},
            ladder=[
                ("constraint", False, f"{ok_rate:.0%} of these succeed — there is no failure to prevent; "
                                      "the cost is re-authoring, and a rule cannot create a capability"),
                ("skill", True, "stateless and one call deep: a CLI plus a skill naming it closes it "
                                "in the parent's own context"),
                ("subagent", False, "not needed — nothing here needs isolated state or its own context"),
            ],
            correction=[_sample(e) for e in evs[:3]],
            samples=_recent(evs),
            requires=["human-review"],
        )
        c.gate("breadth", reuse != tiers.LOCAL,
               f"{len(sessions)} sessions over {weeks} weeks (need >= {tiers.REUSE_THRESHOLDS[tiers.RECURRING][0]} "
               f"sessions and >= {tiers.REUSE_THRESHOLDS[tiers.RECURRING][1]} weeks)")
        c.gate("works", ok_rate >= MIN_SUCCESS,
               f"{ok_rate:.0%} succeed; a capability built from a failing pattern belongs on the constraint side")
        c.proposal = {
            "skill_md": _skill_md(root, verbs, observed_cli, len(evs), len(sessions), weeks, mean_chars),
            "cli_verbs": [v for v, _ in verbs.most_common(coverage.get(0.95) or 15)],
            "companion_nudge": {
                "id": f"nudge-inline-{root}", "tool": "Bash", "field": "command", "action": "monitor",
                "any": [rf"(?:python3?\s+(?:-c|-\s*<<)|<<).*(?:from\s+{re.escape(root)}\s+import|import\s+{re.escape(root)}\b)"],
                "message": f"There is a CLI for `{root}` — call it instead of writing a program to import it.",
                "meta": {"derived_by": "runtune", "pairs_with": c.id,
                         "note": "arm only after the CLI ships; a nudge toward a tool that does not exist "
                                 "is how callusguard's fetch-dead-domain rule raised failures 7%→17%"}},
        }
        c.caveats.append("frequency over recorded commands; verb ranking reads truncated command text")
        if exists:
            c.caveats.append("CLI already exists: measure adoption with `runtune measure`, don't build it twice")
        (out if c.admitted else withheld).append(c)
    return out, withheld


def _coverage_curve(verbs: Counter) -> dict:
    total = sum(verbs.values())
    out, run = {}, 0
    for i, (_, n) in enumerate(verbs.most_common(), 1):
        run += n
        for q in (0.8, 0.95):
            if q not in out and total and run / total >= q:
                out[q] = i
    return out


def _observed_invocations(root: str, cli_events) -> list:
    """The CLI's subcommands as agents actually typed them — `toolkit db master`, `toolkit crm tasks`.
    Only syntax that appeared in a SUCCESSFUL call is ever put in front of an agent."""
    seen = Counter()
    for e in cli_events:
        if not e.ok:
            continue
        toks = e.text.split()
        for i, t in enumerate(toks):
            base = t.rsplit("/", 1)[-1]
            if base in (root, f"{root}.py"):
                sub = [x for x in toks[i + 1:i + 3] if re.match(r"^[a-z][a-z0-9_-]*$", x)]
                if sub:
                    # keep the command exactly as typed (`scripts/toolkit`, not `toolkit`): a path
                    # the agent drops is a "command not found" the skill caused
                    head = " ".join(toks[max(0, i - 1):i + 1]) if base.endswith(".py") and i else t
                    seen[" ".join([head] + sub)] += 1
                break
    return [[k, v] for k, v in seen.most_common(15)]


def _skill_md(root, verbs, observed_cli, n, sessions, weeks, mean_chars) -> str:
    """The skill names ONLY commands observed succeeding. Inventing CLI syntax from
    function names would point agents at commands that may not exist — the failure
    mode of a nudge toward a flag the tool does not have."""
    lines = ["---", f"name: use-{root}-cli",
             f"description: Use when you need `{root}` from the shell — call its CLI, don't write python3 -c",
             "---", "", f"# `{root}` from the shell", "",
             f"Agents wrote {n:,} inline programs across {sessions} sessions / {weeks} weeks just to import "
             f"`{root}`" + (f" (mean {mean_chars} chars)" if mean_chars else "") + "."]
    if observed_cli:
        lines += ["Call the CLI instead. These invocations have succeeded in recorded runs:", ""]
        lines += [f"- `{cmd} …`" for cmd, _ in observed_cli[:10]]
    else:
        lines += ["", "<!-- DRAFT: no CLI exists yet. Build one covering the functions below, then list its "
                      "real commands here. Do not arm the companion nudge before it ships. -->"]
    lines += ["", "Functions agents reached for most (what the CLI must cover):", ""]
    lines += [f"- `{v}()` — {c} snippets" for v, c in verbs.most_common(8)]
    lines += ["", "If what you need is not covered, import the library — and say so, so it can be added."]
    return "\n".join(lines) + "\n"


def derive_procedures(corpus, top: int = 10) -> tuple[list, list]:
    """Recurring, all-successful triples of distinct command shapes within one agent."""
    per_agent = defaultdict(list)
    for e in corpus.events:
        if e.kind == "tool":
            per_agent[(e.source, e.invocation or e.session)].append(e)
    triples = defaultdict(list)
    for evs in per_agent.values():
        evs.sort(key=lambda e: e.ts)
        seq = [e for e in evs if e.surface not in _GENERIC and e.shape
               and not shapes.is_exploratory(e.shape) and not e.inline_target]
        seen = set()
        for a, b, c in zip(seq, seq[1:], seq[2:]):
            key = (a.shape, b.shape, c.shape)
            if len(set(key)) < 3 or not (a.ok and b.ok and c.ok) or key in seen:
                continue
            seen.add(key)
            triples[key].append(a)
    ranked = sorted(triples.items(), key=lambda kv: -len({e.session for e in kv[1]}))
    out, withheld = [], []
    for key, firsts in ranked[: top * 4]:
        sessions = {e.session for e in firsts}
        weeks = _weeks(firsts)
        reuse = tiers.classify_reuse(len(sessions), weeks)
        c = Candidate(
            kind="capability", key="procedure|" + " → ".join(key),
            title="procedure: " + " → ".join(f"`{k}`" for k in key),
            claim=f"this three-step sequence succeeded end-to-end in {len(sessions)} sessions over {weeks} weeks.",
            tier=reuse, sources=sorted({e.source for e in firsts}),
            numbers={"sessions": len(sessions), "weeks": weeks, "occurrences": len(firsts)},
            ladder=[("constraint", False, "every step succeeded — nothing to prevent"),
                    ("skill", True, "a procedure the parent runs itself"),
                    ("subagent", False, "no evidence the steps need isolated context")],
            requires=["human-review"],
            caveats=["a recurring sequence, not an understood one — read the samples before drafting"],
        )
        c.gate("breadth", reuse == tiers.ESTABLISHED,
               f"{len(sessions)} sessions / {weeks} weeks; procedures need the established tier "
               "because a triple recurs by chance far more easily than a single command")
        (out if c.admitted else withheld).append(c)
        if len(out) >= top:
            break
    return out, withheld


def _sample(e) -> dict:
    return {"source": e.source, "session": e.session, "actor": e.actor, "ts": e.ts.isoformat(),
            "text": e.text[:240], "ok": e.ok}


def _recent(evs, n: int = 20) -> list:
    """The newest root-agent attempt from each of the `n` most recent sessions: the
    requests `verify --init` drafts tasks from, while their transcripts still exist."""
    out, seen = [], set()
    for e in sorted(evs, key=lambda e: e.ts, reverse=True):
        if e.actor == "root" and e.session not in seen:
            seen.add(e.session)
            out.append(_sample(e))
        if len(out) >= n:
            break
    return out
