"""Read Codex CLI rollout files (~/.codex/sessions/**/rollout-*.jsonl) into RunTune events.

A rollout is a JSONL log per session. The pieces used here:

    session_meta                   -> session id, sub-agent type
    turn_context                   -> the model in use
    response_item function_call    -> the tool call and its arguments
    response_item *_call_output    -> its output text
    event_msg exec_command_end     -> the authoritative exit code, when present

Outcome is settled only when the file says so: an exit code, "Script completed" /
"Script failed", or a patch-apply result. A call whose outcome the file never
states (backgrounded, rollout closed first) is skipped, not counted as a success.
Files already read are remembered by (size, mtime), so a scheduled run does no
work when nothing changed.
"""

from __future__ import annotations

import glob
import json
import os
import re

from .. import home
from ..evidence.redact import redact

_EXIT = re.compile(r"(?:Process exited with code|Exit code:)\s*(-?\d+)")


def _outcome(text: str) -> tuple[bool | None, str]:
    head = (text or "")[:3000]
    if "rejected" in head.lower() and "user" in head.lower():
        return False, "rejected by user/guardian"
    m = _EXIT.search(head)
    if m:
        code = int(m.group(1))
        tail = [ln for ln in head.splitlines() if ln.strip()][-1:] if code else []
        return code == 0, (tail[0][:300] if tail else "")
    s = head.lstrip()
    if s.startswith("{"):
        try:
            md = json.loads(text).get("metadata") or {}
            if isinstance(md.get("exit_code"), int):
                return md["exit_code"] == 0, ""
        except (ValueError, AttributeError):
            pass
    if s.startswith("Script failed"):
        return False, s[:300]
    if s.startswith("Script completed") or "Success. Updated the following files" in head:
        return True, ""
    return None, ""


def _flatten(out) -> str:
    """Tool output arrives as a string, a {"content": ...} dict, or a list of content
    blocks. Only the text decides the outcome, so reduce every shape to text."""
    if isinstance(out, str):
        return out
    if isinstance(out, dict):
        if isinstance(out.get("metadata"), dict):
            return json.dumps(out)
        return _flatten(out.get("content") or out.get("output") or out.get("text") or "")
    if isinstance(out, list):
        return "\n".join(_flatten(x.get("text", x) if isinstance(x, dict) else x) for x in out)
    return "" if out is None else str(out)


def _command(args) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    if isinstance(args, dict):
        c = args.get("cmd") or args.get("command")
        if isinstance(c, list):
            return c[-1] if len(c) >= 3 and c[1] in ("-lc", "-c") else " ".join(map(str, c))
        if isinstance(c, str):
            return c
        return json.dumps(args)[:500]
    return str(args or "")


def parse(path: str) -> list[dict]:
    session, subagent, model = None, None, None
    calls: dict[str, dict] = {}
    order: list[str] = []
    exit_codes: dict[str, int] = {}
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t, p, ts = o.get("type"), o.get("payload") or {}, o.get("timestamp")
            if t == "session_meta":
                session = p.get("id")
                src = p.get("source")
                if isinstance(src, dict):
                    sa = src.get("subagent")
                    subagent = sa if isinstance(sa, str) else (list(sa)[0] if isinstance(sa, dict) and sa else None)
            elif t == "turn_context" and p.get("model"):
                model = p["model"]
            elif t == "event_msg" and p.get("type") == "exec_command_end" and p.get("call_id"):
                if isinstance(p.get("exit_code"), int):
                    exit_codes[p["call_id"]] = p["exit_code"]
            elif t == "response_item" and p.get("type") in ("function_call", "custom_tool_call"):
                cid = p.get("call_id") or p.get("id")
                calls[cid] = {"tool": p.get("name"), "text": _command(p.get("arguments", p.get("input"))),
                              "ts": ts, "model": model}
                order.append(cid)
            elif t == "response_item" and p.get("type") in ("function_call_output", "custom_tool_call_output"):
                cid = p.get("call_id")
                if cid in calls:
                    calls[cid]["out"] = _flatten(p.get("output"))
    events = []
    for cid in order:
        c = calls[cid]
        if cid in exit_codes:
            ok, err = exit_codes[cid] == 0, ("" if exit_codes[cid] == 0 else f"exit {exit_codes[cid]}")
        else:
            ok, err = _outcome(c.get("out", ""))
        if ok is None or not c.get("ts"):
            continue
        ev = {"type": "event", "source": "codex", "session": session or os.path.basename(path),
              "ts": c["ts"], "surface": c["tool"] or "?", "text": redact(c["text"], 1500), "ok": ok,
              "actor": subagent or "root", "invocation": session if subagent else None, "model": c["model"]}
        if not ok:
            ev["error"] = redact(err or "failed", 400)
        events.append(ev)
    return events


def ingest(root: str = "~/.codex/sessions") -> dict:
    state_path = home.path("recorder-state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    seen = state.setdefault("codex", {})
    files = glob.glob(os.path.join(os.path.expanduser(root), "**", "rollout-*.jsonl"), recursive=True)
    n_files = n_events = 0
    out_dir = home.path("events")
    os.makedirs(out_dir, exist_ok=True)
    for p in sorted(files):
        st = os.stat(p)
        sig = [st.st_size, int(st.st_mtime)]
        if seen.get(p) == sig:
            continue
        evs = parse(p)
        already = seen.get(p + "#n", 0)
        new = evs[already:]
        for ev in new:
            with open(os.path.join(out_dir, f"{ev['ts'][:10]}.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")
        seen[p], seen[p + "#n"] = sig, len(evs)
        n_files += 1
        n_events += len(new)
    os.makedirs(home.root(), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)
    return {"files_read": n_files, "events_written": n_events, "files_seen": len(files)}
