"""Standing: an applied skill or sub-agent is on trial until distinct sessions show it working.

Review asks whether an artifact moved a rate against a control. Standing asks a narrower
question, one session at a time: when agents used this, did it work? Adapted from
@parcha/pi-agent-learn's workflow standing (promotion on receipts from distinct runs, demotion
that restores the revision it replaced), with two changes: a use's outcome comes from recorded
rows, never from the agent's own report, and a demotion is a proposal a person approves.

A USE is one session, after the artifact went live, in which it was exercised:
  capability (adoption case)  the CLI the skill points to ran in the session
  subagent   (agent_type)     the agent type was invoked in the session
A session counts once, by how its LAST use ended: a command that failed and then worked on a
retry is a clean session; one that ended failing is a bad one. An invocation that completed is
clean; failed or orphaned is bad. Sessions the artifact was derived from (before it went live)
are no evidence for it.

  trial  -> proven    the last `promote_after` uses (default 3) are all clean
  any    -> demote    the last `demote_after` uses (default 2) are all bad: a proposal to retire
                      it and, when it revised an earlier artifact, restore that one

Promotion changes no file; it is recorded in the ledger. Constraints and routes have no
standing: their tiers, control-adjusted review and route evals already govern them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

KINDS = ("capability", "subagent")
DEFAULTS = {"promote_after": 3, "demote_after": 2}


@dataclass(frozen=True)
class Use:
    session: str
    at: datetime
    clean: bool


def _dt(s):
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def live_since(art) -> datetime | None:
    """When the change could first have been used: its apply, or the merge it waited for."""
    if art.get("pending_publish") or art.get("pending_merge"):
        return None
    at = art.get("live_at") or art.get("applied_at")
    return _dt(at) if at else None


def uses(art, corpus) -> list[Use]:
    since = live_since(art)
    if since is None:
        return []
    per: dict[str, list[tuple[datetime, bool]]] = {}
    for case in art.get("cases", []):
        t = case.get("type")
        if t == "adoption":
            root = case["root"]
            for e in corpus.events:
                if e.ts < since or e.inline_target:
                    continue
                if root in {s[:-3] if s.endswith(".py") else s for s in e.shape.split(" ")}:
                    per.setdefault(e.session, []).append((e.ts, bool(e.ok)))
        elif t == "agent_type":
            for i in corpus.invocations:
                if i.agent_type == case["name"] and i.started and i.started >= since and i.status != "pending":
                    per.setdefault(i.session, []).append((i.started, i.status == "completed"))
    out = [Use(s, max(t for t, _ in evs), sorted(evs)[-1][1]) for s, evs in per.items()]
    return sorted(out, key=lambda u: u.at)


def settle(art, corpus, cfg: dict | None = None) -> dict:
    """{standing, decision: promote|demote|hold, uses, clean, bad, why}."""
    c = {**DEFAULTS, **(cfg or {})}
    us = uses(art, corpus)
    standing = art.get("standing", "trial")
    last_p, last_d = us[-c["promote_after"]:], us[-c["demote_after"]:]
    res = {"standing": standing, "uses": len(us), "clean": sum(u.clean for u in us),
           "bad": sum(not u.clean for u in us), "decision": "hold", "why": ""}
    if len(last_d) == c["demote_after"] and not any(u.clean for u in last_d):
        res.update(decision="demote", why=f"the last {c['demote_after']} sessions that used it ended badly")
    elif standing == "trial" and len(last_p) == c["promote_after"] and all(u.clean for u in last_p):
        res.update(decision="promote", why=f"{c['promote_after']} distinct sessions used it cleanly")
    else:
        res["why"] = f"{len(us)} session(s) since it went live"
    return res
