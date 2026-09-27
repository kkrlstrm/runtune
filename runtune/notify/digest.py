"""Propose -> notify -> reply -> apply. How RunTune's suggestions reach a person.

Nothing is applied automatically. A weekly `runtune notify` sends ONE digest — a
card image plus a numbered list — to Slack (a DM), email, or stdout. The reader
replies with the numbers they approve. `runtune inbox` (hourly, cheap when idle)
reads replies from the approver only and acts:

    "1, 3"            approve 1 and 3
    "all" / "all except 2" / "all but 2"
    "none"            decline everything in this digest

Anything else — "all but number three", "the first two", "yes to the psql one" —
is AMBIGUOUS, and the answer is a clarifying question in the thread, never a
guess. Two approval loops in the system this came from parsed "all but Number
three" as approve-all; a parser that fails open on a partial approval applies
the thing the person just said no to.

Two classes of item are never applied from a reply, whatever it says:
  * WIDENING items (a looser constraint, a broader grant, a model admitted to a
    mode). They need `runtune apply --reason` — and `--eval` for routes — from a
    terminal, and the reply confirmation says so.
  * items whose candidate changed or whose target moved since the digest (the
    promoter's stale-target refusal applies unchanged).

Declined items are recorded and not proposed again for `SNOOZE_DAYS`.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from ..lifecycle import ledger, promoter

SNOOZE_DAYS = 28
MAX_ITEMS = 8
KIND_ORDER = {"route": 0, "constraint": 1, "capability": 2, "subagent": 3}
_VOLUME_KEYS = ("fails", "events", "agents", "calls", "unlogged_requests", "billed_outside_router", "invocations")

_ALLOWED = {"all", "none", "no", "except", "but", "and", "approve", "apply", "yes", "ok", "okay",
            "please", "thanks", "thank", "you", "items", "item", "skip", "go", "ahead", "with"}
_WORD_NUMBERS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                 "first", "second", "third", "fourth", "fifth", "last"}


def parse_reply(text: str, n_max: int) -> dict:
    """Strict. Returns {status: ok|ambiguous, approve: [n...], why}."""
    t = (text or "").lower().strip()
    t = re.sub(r"[.!?;:#()\[\]]", " ", t)
    words = re.findall(r"[a-z]+", t)
    if not words and not re.search(r"\d", t):
        return {"status": "ambiguous", "approve": [], "why": "empty reply"}
    bad = [w for w in words if w in _WORD_NUMBERS]
    if bad:
        return {"status": "ambiguous", "approve": [], "why": f"numbers written as words ({', '.join(bad)})"}
    unknown = [w for w in words if w not in _ALLOWED]
    if unknown:
        return {"status": "ambiguous", "approve": [], "why": f"did not understand: {' '.join(unknown[:5])}"}
    nums: list[int] = []
    for a, b in re.findall(r"(\d+)\s*(?:-\s*(\d+))?", t):
        lo, hi = int(a), int(b or a)
        if hi < lo:
            lo, hi = hi, lo
        nums.extend(range(lo, hi + 1))
    out_of_range = [n for n in nums if n < 1 or n > n_max]
    if out_of_range:
        return {"status": "ambiguous", "approve": [], "why": f"no item {out_of_range[0]} (1–{n_max})"}
    ws = set(words)
    if ws & {"none"} or (ws == {"no"} and not nums):
        if nums:
            return {"status": "ambiguous", "approve": [], "why": "'none' together with numbers"}
        return {"status": "ok", "approve": [], "why": "declined all"}
    if "all" in ws:
        if nums and not ws & {"except", "but", "skip"}:
            return {"status": "ambiguous", "approve": [], "why": "'all' together with numbers but no 'except'"}
        return {"status": "ok", "approve": [n for n in range(1, n_max + 1) if n not in nums],
                "why": "all" + (f" except {sorted(set(nums))}" if nums else "")}
    if ws & {"except", "but", "skip"}:
        return {"status": "ambiguous", "approve": [], "why": "an exclusion without 'all'"}
    if not nums:
        return {"status": "ambiguous", "approve": [], "why": "no item numbers"}
    return {"status": "ok", "approve": sorted(set(nums)), "why": "listed"}


# ------------------------------------------------------------------ compose
def _volume(c) -> float:
    n = c.get("numbers", {})
    return max([float(n.get(k) or 0) for k in _VOLUME_KEYS] + [0])


def existing_rules(ws, target_root: str | None) -> list:
    """Rules already in force in the constraint target, so a digest never re-proposes them."""
    if not target_root:
        return []
    from ..enforce import load_rules
    path = os.path.join(target_root, ws.authority()["targets"]["constraint"])
    return load_rules([path])[0]


def select(ws, run: dict, limit: int = MAX_ITEMS, target_root: str | None = None) -> list[dict]:
    from ..derive.constraints import covered_by
    live = existing_rules(ws, target_root)
    taken = {a["id"] for a in ws.artifacts()}
    snoozed = _snoozed(ws)
    # Offered for one-reply approval only when it can be applied AS PROPOSED. Lint
    # findings are fixed by hand, and a constraint whose own replay says most of what it
    # matches succeeds needs a person to narrow it first — approving it by reply would
    # nudge every call to that command.
    pool = [c for c in run.get("candidates", [])
            if c["id"] not in taken and c["id"] not in snoozed and not needs_judgment(c)
            and not (c["kind"] == "constraint" and covered_by(c, live))]
    pool.sort(key=lambda c: (KIND_ORDER.get(c["kind"], 9), -_volume(c)))
    return pool[:limit]


def needs_judgment(c) -> bool:
    return c["tier"] == "lint" or any("needs a narrower pattern" in cv for cv in c.get("caveats", []))


def _snoozed(ws) -> set:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SNOOZE_DAYS)
    out = set()
    for e in ledger.read(ws.ledger_path):
        if e.get("action") in ("decline", "acknowledge") and datetime.fromisoformat(e["ts"]) > cutoff:
            out.add(e["id"])
    return out


def _meta(c) -> str:
    bits = [c["tier"]]
    if c["direction"] == "widen":
        bits.append("WIDENS — needs `runtune apply --reason` (and --eval), not a reply")
    elif "eval" in c.get("requires", []):
        bits.append("eval required")
    if not c.get("proposal"):
        bits.append("a code fix, not an artifact — approving files it as done-by-hand")
    bits.append(" · ".join(c.get("sources", [])))
    return " · ".join(b for b in bits if b)


def compose(ws, run: dict, review_rows: list, limit: int = MAX_ITEMS, target_root: str | None = None) -> dict:
    items = [{"n": i, "id": c["id"], "kind": c["kind"], "direction": c["direction"],
              "title": c["title"], "meta": _meta(c)}
             for i, c in enumerate(select(ws, run, limit, target_root), 1)]
    worse = [r for r in review_rows if r["verdict"] in ("review", "retire", "probe", "revalidate")]
    bars = []
    for r in review_rows:
        net = (r.get("detail") or {}).get("net_change")
        if net is not None:
            bars.append((r["title"][:40], net))
    cands, held = run.get("candidates", []), run.get("withheld", [])
    if worse:
        chip = (f"{len(worse)} NEED ATTENTION", "#d03b3b", "■")
    elif items:
        chip = (f"{len(items)} TO REVIEW", "#fab219", "▲")
    else:
        chip = ("ALL CLEAR", "#0ca30c", "●")
    cov = run.get("coverage", {})
    gaps = sum(1 for v in cov.values() if "GAP" in v)
    return {
        "title": "RunTune — this week",
        "subtitle": f"{', '.join(run.get('sources', []))} · {run.get('days', '?')} days of runs"
                    + (f" · {gaps} source(s) with recorder gaps" if gaps else ""),
        "chip": chip,
        "tiles": [("Proposed", f"{len(cands)}", f"{len(held)} withheld with a reason"),
                  ("Active", f"{len(ws.artifacts('active'))}", f"{len(worse)} flagged by review"),
                  ("In this digest", f"{len(items)}", "reply with numbers")],
        "bars": bars[:6],
        "items": items,
        "review": [{"id": r["id"], "verdict": r["verdict"], "title": r["title"]} for r in worse],
        "judgment": sum(1 for c in cands if needs_judgment(c)),
        "footer": "Reply in thread: “1,3” · “all” · “all except 2” · “none”. Anything else gets a question back, "
                  "never a guess. Widening items are applied from a terminal only.",
    }


def to_text(d: dict) -> str:
    lines = [f"*{d['title']}* — {d['chip'][0]}", d["subtitle"], ""]
    for k, h, s in d["tiles"]:
        lines.append(f"• {k}: *{h}* ({s})")
    if d["review"]:
        lines += ["", "*Review flagged:*"] + [f"• {r['verdict']}: {r['title'][:90]}" for r in d["review"]]
    lines += ["", "*Proposals:*"]
    for i in d["items"]:
        lines.append(f"*{i['n']}.* {i['title'][:140]}\n     _{i['meta']}_")
    if d.get("judgment"):
        lines += ["", f"_{d['judgment']} more need a person to narrow or fix them first — see the report "
                      "(`runtune derive`); they are not approvable by reply._"]
    lines += ["", d["footer"]]
    return "\n".join(lines)


# ------------------------------------------------------------------- state
def _dir(ws) -> str:
    p = os.path.join(ws.root, "digests")
    os.makedirs(p, exist_ok=True)
    return p


def save(ws, state: dict) -> None:
    with open(os.path.join(_dir(ws), f"{state['digest_id']}.json"), "w") as f:
        json.dump(state, f, indent=2, default=str)


def awaiting(ws) -> list[dict]:
    out = []
    for fn in sorted(os.listdir(_dir(ws))):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(_dir(ws), fn)) as f:
            s = json.load(f)
        if s.get("status") == "awaiting_reply":
            out.append(s)
    return out


def act(ws, state: dict, reply: str, approver: str, target_root: str) -> dict:
    """Parse one reply and carry it out. Returns {status, applied, refused, declined, message}."""
    items = state["items"]
    parsed = parse_reply(reply, len(items))
    if parsed["status"] != "ok":
        return {"status": "ambiguous", "message": f"I didn't act on that ({parsed['why']}). Reply with item "
                f"numbers like “1,3”, or “all”, “all except 2”, “none”."}
    applied, refused, declined = [], [], []
    for it in items:
        if it["n"] not in parsed["approve"]:
            declined.append(it["n"])
            ledger.append(ws.ledger_path, {"action": "decline", "id": it["id"], "digest": state["digest_id"],
                                           "by": approver})
            continue
        if it["direction"] == "widen":
            refused.append((it["n"], "widens a boundary — apply from a terminal with --reason (and --eval)"))
            continue
        cand = ws.find_candidate(it["id"]) or {}
        if not cand.get("proposal"):
            ledger.append(ws.ledger_path, {"action": "acknowledge", "id": it["id"], "by": approver,
                                           "digest": state["digest_id"], "note": "code fix, tracked by hand"})
            refused.append((it["n"], "no artifact to apply — recorded as acknowledged; the fix is a code change"))
            continue
        try:
            if not ws.get(it["id"]):
                promoter.stage(ws, it["id"], target_root)
            promoter.apply(ws, it["id"], approver=approver, reason=f"approved in digest {state['digest_id']}")
            applied.append(it["n"])
        except promoter.Refused as exc:
            refused.append((it["n"], str(exc)))
    msg = []
    if applied:
        msg.append("Applied: " + ", ".join(map(str, applied)))
    for n, why in refused:
        msg.append(f"Not applied {n}: {why}")
    if declined:
        msg.append(f"Declined (snoozed {SNOOZE_DAYS} days): " + ", ".join(map(str, declined)))
    return {"status": "done", "applied": applied, "refused": refused, "declined": declined,
            "message": "\n".join(msg) or "Nothing to do."}
