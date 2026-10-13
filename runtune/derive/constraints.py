"""FAILURE / DRIFT -> constraint.  "Don't do this again."

This is callusguard's derive stage, generalized across hosts and given the
admission gate it was missing.

What it keeps from callusguard, unchanged in meaning:
  * failures cluster by (surface, command shape, error signature);
  * every cluster is graded against its DENOMINATOR — all attempts of the same
    shape, successes included — and an anecdotal cluster is withheld, not
    proposed, and always reported as withheld;
  * every candidate lands as `monitor`, armed at nothing, with an action ceiling
    set by its tier.

What it adds:
  * ONE SHELL, EVERY HOST. Claude's `Bash`, Codex's `exec`/`exec_command`, Cursor's
    terminal tools (recorded as `shell`) and Antigravity's `run_command` are the same surface. A failure that
    recurs on more than one host is corroborated by independent agents, and the
    candidate says so.
  * BREADTH. A failure repeated 40 times inside one session is one incident in a
    retry loop. A cluster must span >= 2 sessions to be proposed.
  * THE REPLAY GATE (from AutoRefine, made deterministic). The candidate pattern
    is replayed over every historical attempt in the window. Failures it matches
    are its correction cases; successes it matches are its collateral — the
    preservation cases it would disturb. Nothing is executed: this is regex over
    recorded strings. A pattern whose collateral exceeds its tier's allowance
    fails the gate, which is how a `\\bpython3\\b` candidate dies before a human
    ever has to read it.
  * LOW-SIGNAL HEADS (`cd`, `for`, `set`, …) are a failed gate with a reason, not a
    silent filter: callusguard's funnel showed these are real clusters and
    worthless rules, and the reviewer should see that they were considered.
  * EXIT-STATUS ATTRIBUTION. A shell failure is keyed on the part of the command
    whose status the call reported (`a | head; b` -> b), not on its first command.
    Keyed on the head, `report.py | head; echo ===; …` filed the report's normal
    output as its error and proposed a rule against a command that had worked. A
    failure whose part cannot be told (`a && b`, a loop) is reported as withheld.
  * ONE CAUSE, MANY COMMANDS. An error shared by >= 3 different commands (a broken
    interpreter, a missing binary on PATH) is one ENVIRONMENT finding, not one rule
    per command: no rule on a command fixes the machine.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..evidence import shapes, tiers
from ..evidence.redact import redact
from .candidate import TIGHTEN, Candidate

SHELL_SURFACES = {"Bash", "exec", "exec_command", "shell", "local_shell", "run_command"}
MIN_FAILS = 3
MIN_SESSIONS = 2
# Collateral allowance. Above MAX_COLLATERAL the failure is immaterial against the
# traffic the pattern would touch (under 2% of matches) and is withheld. Between
# NARROW_ABOVE and MAX_COLLATERAL it is admitted but flagged: the shape-level pattern
# is too broad to arm, and a human has to narrow it first — which is what every
# promoted callusguard rule needed (`\bpsql\b` -> a bare-invocation pattern).
MAX_COLLATERAL = 0.98
NARROW_ABOVE = 0.50
# An error signature shared by this many different commands is an environment cause.
ENV_MIN_COMMANDS = 3


def surface_family(ev) -> str:
    return "shell" if ev.surface in SHELL_SURFACES else shapes.command_shape(ev.surface, "")


def cluster_key(ev) -> tuple[str, str | None]:
    """(surface, shape) a failure is counted under. For the shell, the shape of the part
    whose exit status the call reported; None when that cannot be told."""
    fam = surface_family(ev)
    if fam != "shell":
        return fam, ev.shape
    return fam, shapes.status_shape(ev.surface, ev.text, truncated=ev.chars > len(ev.text))


def environment_signature(error: str) -> str | None:
    """The part of a normalized error that can name a machine-level cause, or None.

    The exit-code prefix and the shell's own location prefix (`/P:`, `(eval):#:`) say
    nothing, and two shells printing the same cause would otherwise split it. A traceback
    head and a usage line are shared by unrelated failures; a warning line
    (`notopensslwarning …`) is printed on success too; and a harness timeout means each
    command was slow, not that something on the machine is broken. What is left must
    read as an error: a failed chain can record a file's normal contents as its "error"."""
    s = re.sub(r"^exit code #\s*", "", error or "").strip()
    s = re.sub(r"^(?:(?:/P|\(eval\))(?::#)?:?\s+)+", "", s, flags=re.I).strip()
    if (len(s) < 20 or s.startswith(("traceback", "usage")) or "warning" in s[:60]
            or re.search(r"\btimed out\b", s) or not _ERRORISH.search(s)):
        return None
    return s[:120]


_ERRORISH = re.compile(r"error|not found|unable|cannot|can't|denied|failed|no such|missing|invalid|"
                       r"unexpected|refused|not recognized|not installed", re.I)


def candidate_pattern(shape: str) -> str | None:
    """The shape keys on basenames (`python3 fetch-page.py`), but agents type paths
    (`python3 scripts/fetch-page.py`), so every token may carry a directory prefix.
    The first version required adjacent tokens and matched none of its own failures."""
    toks = [t for t in shape.split(" ") if t]
    if not toks:
        return None
    return r"(?:^|[;&|(]\s*|\s|/)" + r"\s+".join(r"(?:\S*/)?" + re.escape(t) for t in toks) + r"(?![\w.-])"


def replay(pattern: str, events, surface: str = "shell", literals=()) -> dict:
    """Replay a pattern over recorded attempts. Returns match counts and samples.

    `matched_fail` are correction cases; `matched_ok` are preservation cases the
    pattern would also have fired on. Pure string matching — nothing runs.
    """
    rx = re.compile(pattern)
    m_fail, m_ok, sessions, hosts = [], [], set(), Counter()
    for e in events:
        if surface_family(e) != surface or not e.text:
            continue
        if literals and not all(t in e.text for t in literals):
            continue
        if rx.search(e.text):
            (m_ok if e.ok else m_fail).append(e)
            sessions.add(e.session)
            hosts[e.source] += 1
    n = len(m_fail) + len(m_ok)
    return {
        "matched": n, "matched_fail": len(m_fail), "matched_ok": len(m_ok),
        "fail_rate": round(len(m_fail) / n, 4) if n else None,
        "sessions": len(sessions), "hosts": dict(hosts),
        "fail_samples": m_fail[:3], "ok_samples": m_ok[:3],
    }


def derive(corpus, min_fails: int = MIN_FAILS, min_sessions: int = MIN_SESSIONS,
           suppress: set | None = None) -> tuple[list, list]:
    """Returns (candidates, withheld). Withheld is every cluster that failed a gate,
    with the reason — "nothing to propose" must never hide "eight too thin to grade"."""
    suppress = suppress or set()
    tool_events = [e for e in corpus.events if e.kind == "tool"]
    shell_events = [e for e in tool_events if e.surface in SHELL_SURFACES]
    keys = {id(e): cluster_key(e) for e in tool_events}
    attempts = Counter(keys[id(e)] for e in tool_events)
    shape_ok = Counter(keys[id(e)] for e in tool_events if e.ok)
    failures = [e for e in tool_events if not e.ok]

    out, withheld = [], []
    explained = _environment(failures, keys, min_fails, min_sessions, suppress, out, withheld)

    # One candidate per (surface, shape): the rule it compiles to is the same regex
    # whatever the error text, so splitting by signature would propose one rule many
    # times. The signatures are kept inside the candidate as its breakdown.
    clusters = defaultdict(list)
    unattributed = []
    for e in failures:
        if id(e) in explained:
            continue
        surface, shape = keys[id(e)]
        if shape is None:
            unattributed.append(e)
        else:
            clusters[(surface, shape)].append(e)
    if len(unattributed) >= min_fails:
        u = Candidate(
            kind="constraint", key="shell|(unattributed)",
            title=f"shell: {len(unattributed)} failed compound commands could not be pinned to one command",
            claim="the exit status of an `&&` chain, a loop or a truncated recording does not say which part "
                  "failed, so these failures are not counted against any command",
            tier=tiers.ANECDOTAL, sources=sorted({e.source for e in unattributed}),
            numbers={"fails": len(unattributed)}, correction=[_sample(e) for e in unattributed[:3]])
        u.gate("attribution", False, "the failing part of the command cannot be identified")
        withheld.append(u)

    for (surface, shape), fails in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        if len(fails) < min_fails:
            continue
        sigs = Counter(e.error for e in fails)
        sig = sigs.most_common(1)[0][0]
        n_att = attempts[(surface, shape)]
        tier, rate = tiers.classify_failure(len(fails), n_att)
        sessions = {e.session for e in fails}
        hosts = Counter(e.source for e in fails)
        c = Candidate(
            kind="constraint", key=f"{surface}|{shape}",
            title=f"{surface}: `{shape or '(empty)'}` → {sig[:70] or '(no error text)'}"
                  + (f" (+{len(sigs) - 1} other error shapes)" if len(sigs) > 1 else ""),
            claim=(f"`{shape}` failed {len(fails)} times across {len(sessions)} sessions; "
                   f"{tiers.describe_failure(tier, rate, n_att)} of that shape."),
            tier=tier, direction=TIGHTEN, sources=sorted(hosts),
            numbers={"fails": len(fails), "attempts": n_att, "shape_successes": shape_ok[(surface, shape)],
                     "sessions": len(sessions), "hosts": dict(hosts),
                     "error_signatures": [[k[:120], v] for k, v in sigs.most_common(6)],
                     "distinct_error_signatures": len(sigs),
                     "first": min(e.ts for e in fails).isoformat(), "last": max(e.ts for e in fails).isoformat(),
                     "action_ceiling": tiers.ceiling(tier)},
            ladder=[("constraint", True, "a check on a single call closes it: the failure is local to one attempt")],
            correction=[_sample(e) for e in fails[:3]],
            requires=["human-review"],
        )
        c.gate("denominator", tier != tiers.ANECDOTAL,
               f"{n_att} attempts of this shape" + ("" if tier != tiers.ANECDOTAL else f" < {tiers.MIN_ATTEMPTS}"),
               suppressed="denominator" in suppress)
        c.gate("breadth", len(sessions) >= min_sessions,
               f"{len(sessions)} session(s); one session retrying is one incident",
               suppressed="breadth" in suppress)
        c.gate("signal", not shapes.is_exploratory(shape),
               f"head `{shape.split(' ')[0] if shape else ''}` is a builtin, an inline interpreter or read-only "
               "exploration — in a chained command the failure is usually a later step, and a rule on it "
               "would fire on everything",
               suppressed="signal" in suppress)

        pattern = candidate_pattern(shape) if surface == "shell" else None
        if pattern:
            rp = replay(pattern, shell_events, literals=shape.split(" "))
            coll = rp["matched_ok"] / rp["matched"] if rp["matched"] else 0.0
            c.numbers["replay"] = {k: v for k, v in rp.items() if not k.endswith("samples")}
            c.preservation = [_sample(e) for e in rp["ok_samples"]]
            # AutoRefine's closure check, offline: a rule must catch the failures it was
            # derived from. Zero matches used to pass the collateral gate (0 of 0).
            c.gate("replay-covers-correction", rp["matched_fail"] >= 0.5 * len(fails),
                   f"pattern matches {rp['matched_fail']} of the {len(fails)} failures it was derived from",
                   suppressed="replay-covers-correction" in suppress)
            c.gate("replay-collateral", coll <= MAX_COLLATERAL,
                   f"pattern matches {rp['matched']} recorded attempts; {rp['matched_ok']} of them succeeded "
                   f"({coll:.0%} collateral)", suppressed="replay-collateral" in suppress)
            if coll > NARROW_ABOVE:
                c.caveats.append(f"needs a narrower pattern before arming: {coll:.0%} of what the "
                                 "shape-level pattern matches succeeded")
            c.proposal = {"ruleset_rule": {
                "id": c.id, "tool": "Bash",
                "field": "command", "any": [pattern], "action": "monitor", "severity": 40,
                "message": f"DRAFT — `{shape}` keeps failing here: {redact(fails[0].error, 140)}. "
                           f"Rewrite this into the working path, then promote no further than "
                           f"{tiers.ceiling(tier)}.",
                "meta": {"derived_by": "runtune", "tier": tier, "fail_rate": round(rate or 0, 4),
                         "attempt_count": n_att, "action_ceiling": tiers.ceiling(tier),
                         "hosts": sorted(hosts), "error_signature": sig,
                         "sample_command": fails[0].text[:200]},
            }}
        else:
            coll = shape_ok[(surface, shape)] / n_att if n_att else 0.0
            c.gate("replay-collateral", True, f"tool-surface constraint: fires on every call of the surface; "
                                               f"{coll:.0%} of those succeed")
            if coll > NARROW_ABOVE:
                c.caveats.append(f"needs a narrower pattern before arming: {coll:.0%} of calls to this tool "
                                 "succeed, so a tool-wide rule would fire mostly on working calls")
            c.proposal = {"ruleset_rule": {
                "id": c.id, "tool": shape or surface, "action": "monitor", "severity": 60,
                "message": f"DRAFT — tool surface `{shape}` keeps failing ({redact(sig, 120)}).",
                "meta": {"derived_by": "runtune", "tier": tier, "attempt_count": n_att,
                         "action_ceiling": tiers.ceiling(tier), "hosts": sorted(hosts)}}}
        if len(hosts) > 1:
            c.caveats.append(f"corroborated on {len(hosts)} hosts ({', '.join(sorted(hosts))})")
        c.caveats.append("observational rate over recorded traffic, not a controlled re-run")
        (out if c.admitted else withheld).append(c)
    return out, withheld


def _sample(e) -> dict:
    return {"source": e.source, "ts": e.ts.isoformat(), "text": e.text[:240],
            "error": e.error[:160], "ok": e.ok}


def _environment(failures, keys, min_fails, min_sessions, suppress, out, withheld) -> set:
    """Group failures that share an environment signature across >= ENV_MIN_COMMANDS
    different commands into one environment finding each. Returns the ids of the
    failures those findings explain, so they are not also proposed per command."""
    by_sig = defaultdict(list)
    for e in failures:
        sig = environment_signature(e.error)
        if sig:
            by_sig[sig].append(e)
    explained = set()
    for sig, fails in sorted(by_sig.items(), key=lambda kv: -len(kv[1])):
        # the part that failed where it can be told, else the command's head
        commands = Counter((keys[id(e)][0], keys[id(e)][1] or e.shape) for e in fails)
        if len(commands) < ENV_MIN_COMMANDS or len(fails) < min_fails:
            continue
        sessions = {e.session for e in fails}
        hosts = Counter(e.source for e in fails)
        c = Candidate(
            kind="environment", key=f"env|{sig[:80]}",
            title=f"environment: `{sig[:70]}` breaks {len(commands)} different commands",
            claim=(f"{len(fails)} failures across {len(sessions)} sessions share one error in {len(commands)} "
                   "different commands. The cause is the machine or toolchain, not any one command, so a rule "
                   "on each command would not fix it."),
            tier=tiers.RECURRING, sources=sorted(hosts),
            numbers={"fails": len(fails), "sessions": len(sessions), "hosts": dict(hosts),
                     "commands": [[f"{s}: {sh}", n] for (s, sh), n in commands.most_common(8)],
                     "error_signature": sig,
                     "first": min(e.ts for e in fails).isoformat(), "last": max(e.ts for e in fails).isoformat()},
            ladder=[("constraint", False, f"a rule keys on one command; this error breaks {len(commands)}"),
                    ("environment fix", True, "fix it once where it lives: the interpreter, PATH or toolchain")],
            correction=[_sample(e) for e in fails[:3]],
            requires=["human-fix"],
            caveats=["no artifact: approving it records the fix as done by hand"])
        c.gate("breadth", len(sessions) >= min_sessions,
               f"{len(sessions)} session(s); one session retrying is one incident",
               suppressed="breadth" in suppress)
        (out if c.admitted else withheld).append(c)
        explained.update(id(e) for e in fails)
    return explained


def replay_ruleset(rules: list, corpus) -> list:
    """Replay an existing ruleset (callusguard format) against history, every host.

    For every Bash rule: how many recorded attempts it matches, how many of those
    failed, and how many were successes it would have caught in the crossfire. A
    rule that matches nothing in the window is a `probe` question, not a verdict.
    """
    tool_events = [e for e in corpus.events if e.kind == "tool"]
    out = []
    for r in rules:
        if r.get("tool") not in ("Bash", None) or r.get("field", "command") != "command":
            continue
        pats = r.get("any") or ([r["pattern"]] if r.get("pattern") else [])
        if not pats:
            continue
        combined = "|".join(f"(?:{p})" for p in pats)
        try:
            rp = replay(combined, tool_events)
        except re.error as exc:
            out.append({"id": r.get("id"), "error": f"pattern does not compile: {exc}"})
            continue
        tier, _ = tiers.classify_failure(rp["matched_fail"], rp["matched"])
        action = r.get("action", "monitor")
        out.append({"id": r.get("id"), "action": action, "matched": rp["matched"],
                    "matched_fail": rp["matched_fail"], "fail_rate": rp["fail_rate"],
                    "hosts": rp["hosts"], "tier_now": tier,
                    "over_ceiling": not tiers.within_ceiling(action, tier) and rp["matched"] > 0})
    return out


def covered_by(cand: dict, rules: list) -> str | None:
    """The id of an existing rule that already covers this candidate, or None.

    A tool-surface candidate is covered by any rule on that tool surface. A shell
    candidate is covered when an existing rule's patterns match most of the failures
    the candidate was derived from — the same test the replay gate uses."""
    tool = cand.get("proposal", {}).get("ruleset_rule", {}).get("tool", "")
    samples = [s.get("text", "") for s in cand.get("correction", []) if s.get("text")]
    for r in rules:
        rt = r.get("tool") or ""
        if tool != "Bash" and rt and (rt == tool or (rt.endswith("*") and tool.startswith(rt[:-1]))
                                      or (tool.endswith("*") and rt.startswith(tool[:-1]))):
            return r.get("id")
        if tool == "Bash" and samples and rt in ("Bash", ""):
            pats = r.get("any") or ([r["pattern"]] if r.get("pattern") else [])
            try:
                hit = sum(1 for t in samples if any(re.search(p, t) for p in pats))
            except re.error:
                continue
            if hit and hit >= len(samples) / 2:
                return r.get("id")
    return None
