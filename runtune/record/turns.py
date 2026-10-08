"""Find the turn behind one recorded attempt: what the user asked, what the command
printed, and what the agent answered.

Events keep the command and its outcome, not the conversation around it, and that stays
true: nothing here is written to the event store. `runtune verify --init` calls `find` for
a candidate's samples to propose tasks drawn from real requests, while the host's own
transcript still exists:

    claude       ~/.claude/projects/*/<session>.jsonl          deleted after cleanupPeriodDays (30)
    codex        ~/.codex/sessions/**/rollout-*<session>.jsonl
    cursor       the state.vscdb composer <session>
    antigravity  ~/.gemini/antigravity/brain/<session>/.system_generated/logs/transcript.jsonl

Each host is reduced to one linear list of steps, and one function reads a turn from it:

    ("user", text, ts)  ("call", id, text, ts)  ("output", id, text)  ("answer", text, ts)

Only the root agent's turns are read; a sub-agent's prompt is written by its parent, not
by a person.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone

from ..evidence.redact import redact

_NOT_A_REQUEST = re.compile(r"^\s*(?:<[\w-]+[\s>]|Caveat:|\[Request interrupted|# AGENTS\.md instructions)")
_INJECTED = re.compile(r"^\s*<([\w-]+)[^>]*>.*?</\1>\s*", re.S)


def request(text: str) -> str:
    """What the person typed: the text with the host's injected context blocks
    (`<ide_selection>…</ide_selection>`, `<environment_context>…`) removed from the front.
    Empty when nothing a person wrote is left."""
    text = text or ""
    while True:
        m = _INJECTED.match(text)
        if not m:
            break
        text = text[m.end():]
    text = text.strip()
    return "" if not text or _NOT_A_REQUEST.match(text) else text


def _ts(v) -> datetime | None:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _jsonl(path: str):
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _blocks(content, kind: str) -> str:
    if isinstance(content, str):
        return content if kind == "text" else ""
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") in (kind, "input_text", "output_text"))
    return ""


# ------------------------------------------------------------------ per host
def _claude(session: str, root: str = "~/.claude/projects") -> list:
    paths = glob.glob(os.path.join(os.path.expanduser(root), "*", f"{session}.jsonl"))
    if not paths:
        return []
    from .claude import _result_text, _text
    steps = []
    for o in _jsonl(paths[0]):
        m, ts = o.get("message") or {}, o.get("timestamp")
        content = m.get("content")
        if o.get("type") == "user" and not o.get("isMeta"):
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        steps.append(("output", b.get("tool_use_id"), _result_text(b.get("content"))))
            text = _blocks(content, "text")
            if text.strip():
                steps.append(("user", text, ts))
        elif o.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    steps.append(("call", b.get("id"), _text(b.get("input")), ts))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    steps.append(("answer", b["text"], ts))
    return steps


def _codex(session: str, root: str = "~/.codex/sessions") -> list:
    paths = glob.glob(os.path.join(os.path.expanduser(root), "**", f"rollout-*{session}.jsonl"), recursive=True)
    if not paths:
        return []
    from .codex import _command, _flatten
    steps = []
    for o in _jsonl(paths[0]):
        t, p, ts = o.get("type"), o.get("payload") or {}, o.get("timestamp")
        # older rollouts write the conversation as event_msg, newer ones as response_item
        # messages; a rollout with both repeats each message, which a turn tolerates
        if t == "event_msg" and p.get("type") == "user_message":
            steps.append(("user", p.get("message") or "", ts))
        elif t == "event_msg" and p.get("type") == "agent_message":
            steps.append(("answer", p.get("message") or "", ts))
        elif t == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            steps.append(("user", _blocks(p.get("content"), "input_text"), ts))
        elif t == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            steps.append(("answer", _blocks(p.get("content"), "output_text"), ts))
        elif t == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
            steps.append(("call", p.get("call_id") or p.get("id"),
                          _command(p.get("arguments", p.get("input"))), ts))
        elif t == "response_item" and p.get("type") in ("function_call_output", "custom_tool_call_output"):
            steps.append(("output", p.get("call_id"), _flatten(p.get("output"))))
    return steps


def _antigravity(session: str, root: str = "~/.gemini/antigravity/brain") -> list:
    path = os.path.join(os.path.expanduser(root), session, ".system_generated", "logs", "transcript.jsonl")
    if not os.path.exists(path):
        return []
    from .antigravity import _text
    steps, pending = [], []
    for o in _jsonl(path):
        ts, src, typ = o.get("created_at") or o.get("timestamp"), o.get("source"), o.get("type")
        content = o.get("content") or ""
        if typ == "USER_INPUT":
            steps.append(("user", re.sub(r"</?USER_REQUEST>", "", content).strip(), ts))
        elif o.get("tool_calls"):
            for i, tc in enumerate(o["tool_calls"]):
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                cid = f"{o.get('step_index')}:{i}"
                steps.append(("call", cid, _text(args), ts))
                pending.append(cid)
        elif pending and (typ in ("GENERIC", "ERROR_MESSAGE", "STEP_RESULT") or src == "SYSTEM"):
            steps.append(("output", pending.pop(0), o.get("error") or content))
        elif src == "MODEL" and typ == "PLANNER_RESPONSE" and content.strip():
            steps.append(("answer", content, ts))
    return steps


def _cursor(session: str, store: str | None = None) -> list:
    from . import cursor
    path = os.path.expanduser(store or cursor.default_store())
    if not os.path.exists(path):
        return []
    con = cursor.connect(path)
    try:
        meta = next((m for m in cursor.composers(con) if m.get("composerId") == session), None)
        if not meta:
            return []
        bubbles = cursor._bubbles(con, session)
    finally:
        con.close()
    order = [h.get("bubbleId") for h in meta.get("fullConversationHeadersOnly") or [] if h.get("bubbleId") in bubbles]
    steps = []
    for bid in order:
        b = bubbles[bid]
        ts = b.get("createdAt")
        tf = b.get("toolFormerData")
        if isinstance(tf, dict) and (tf.get("name") or tf.get("toolCallId")):
            cid = tf.get("toolCallId") or bid
            steps.append(("call", cid, cursor._text(tf), ts))
            res = cursor._loads(tf.get("result"))
            out = (res.get("output") or res.get("contents") or "") if isinstance(res, dict) else res
            steps.append(("output", cid, out if isinstance(out, str) else json.dumps(out or "")))
        elif b.get("type") == 1 and (b.get("text") or "").strip():
            steps.append(("user", b["text"], ts))
        elif b.get("type") == 2 and (b.get("text") or "").strip():
            steps.append(("answer", b["text"], ts))
    return steps


HOSTS = {"claude": _claude, "codex": _codex, "antigravity": _antigravity, "cursor": _cursor}


# ------------------------------------------------------------------ the turn
def turn(steps: list, ts, text: str) -> dict | None:
    """The turn around the call recorded at `ts` with `text`. -> {prompt, command, output,
    answer, prompt_ts} or None. The call is matched by time (within 2 s) and, among those,
    by its recorded text; recorded text is redacted and truncated, so it is compared that way."""
    want = _ts(ts)
    calls = [(i, s) for i, s in enumerate(steps) if s[0] == "call"]
    near = [(i, s) for i, s in calls if want and _ts(s[3]) and abs((_ts(s[3]) - want).total_seconds()) <= 2]
    exact = [(i, s) for i, s in near if redact(s[2], 1500)[:240] == text[:240]]
    hit = (exact or [(i, s) for i, s in near if s[2][:60] and text.startswith(redact(s[2], 1500)[:60])] or [None])[0]
    if hit is None:
        return None
    i, call = hit
    prompt = next(((request(s[1]), s[2]) for s in reversed(steps[:i]) if s[0] == "user" and request(s[1])), None)
    if prompt is None:
        return None
    nxt = next((j for j in range(i + 1, len(steps)) if steps[j][0] == "user" and request(steps[j][1])),
               len(steps))
    start = max(j for j in range(i) if steps[j][0] == "user" and request(steps[j][1]))
    output = next((s[2] for s in steps[i + 1:nxt] if s[0] == "output" and s[1] == call[1]), "")
    answers = [s[1] for s in steps[i + 1:nxt] if s[0] == "answer" and s[1].strip()]
    return {"prompt": prompt[0], "prompt_ts": prompt[1], "command": call[2], "output": output,
            "answer": answers[-1].strip() if answers else "",
            # how directly the request led to this call: its position among the turn's calls
            "call_index": sum(s[0] == "call" for s in steps[start:i]),
            "turn_calls": sum(s[0] == "call" for s in steps[start:nxt]),
            # earlier requests in the session: a later one may lean on what was said before it
            "turn_index": sum(s[0] == "user" and bool(request(s[1])) for s in steps[:start])}


def _turn_at(steps: list, i: int) -> dict | None:
    return turn(steps, steps[i][3], redact(steps[i][2], 1500))


def matching(ref: dict, pattern: str) -> list:
    """Every turn in `ref`'s session with a call whose text matches `pattern`, most direct
    first (the call earliest in its turn). Lets a caller pick the cleanest request in a
    session rather than the one call a sample happened to point at."""
    fn = HOSTS.get(ref.get("source"))
    if not fn or not ref.get("session"):
        return []
    try:
        steps = fn(ref["session"])
        rx = re.compile(pattern, re.S)
    except (OSError, ValueError, KeyError, re.error):
        return []
    out = [t for i, s in enumerate(steps) if s[0] == "call" and rx.search(s[2] or "")
           for t in [_turn_at(steps, i)] if t]
    return sorted(out, key=lambda t: (t["call_index"], t["turn_calls"]))


def find(ref: dict) -> dict | None:
    """`ref` is a candidate sample: {source, session, ts, text}. None when the host's
    transcript is gone, the session is unknown, or the call is not in it."""
    fn = HOSTS.get(ref.get("source"))
    if not fn or not ref.get("session"):
        return None
    try:
        steps = fn(ref["session"])
    except (OSError, ValueError, KeyError):
        return None
    return turn(steps, ref.get("ts"), ref.get("text", "")) if steps else None
