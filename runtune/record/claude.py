"""Backfill Claude Code history from its own transcripts (~/.claude/projects/**.jsonl).

The hook records from the day it is installed; this reads what Claude Code already
kept, so a new install has evidence on day one. Claude Code deletes transcripts
after `cleanupPeriodDays` (30 by default), so the backfill reaches back that far.

    assistant record  -> tool_use blocks: the call (id, name, input) and the model
    user record       -> tool_result blocks: the outcome (is_error, content)
    subagents/agent-<id>.jsonl + .meta.json  -> the same, with the sub-agent's type,
                         and what it was asked to do: the description, the parent's
                         Agent call (an exact link), the workflow run and phase, and
                         the first user message (its task), redacted
    <session>/workflows/wf_*.json            -> one record per Workflow run
    cross-session messages                   -> a `message` record from each end: the
                         sender's SendMessage result and the receiver's `origin` share
                         Claude Code's msg_id, and the loader joins the two

A call with no result in the file is unsettled and skipped. Files already read are
remembered by (size, mtime) and only new calls are appended.
"""

from __future__ import annotations

import glob
import json
import os
from collections import Counter

from .. import home
from ..evidence.redact import redact

_TEXT_KEYS = ("command", "file_path", "path", "url", "query", "pattern", "skill", "description")


def _text(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp or "")
    for k in _TEXT_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _peer_origin(origin) -> dict | None:
    """`origin` when it records a message from ANOTHER session. A sub-agent handing its
    report back (`handback`) and a background sub-agent messaging its own parent
    (`senderTaskId`) share kind "peer" and are not cross-session."""
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None
    if origin.get("handback") or origin.get("senderTaskId"):
        return None
    return origin


def _first_line(body: str) -> str:
    return next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")


def parse(path: str) -> list[dict]:
    agent_type = "root"
    meta_path = path[:-len(".jsonl")] + ".meta.json"
    meta: dict = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f) or {}
            agent_type = meta.get("agentType") or "subagent"
        except (ValueError, OSError):
            agent_type = "subagent"
    elif "/subagents/" in path:
        agent_type = "workflow-subagent" if "/workflows/" in path else "subagent"
    parent_dir = os.path.basename(os.path.dirname(path))
    workflow_run = parent_dir if parent_dir.startswith("wf_") else None
    prompt = None
    sends: dict[str, dict] = {}
    messages: list[dict] = []
    calls: dict[str, dict] = {}
    order: list[str] = []
    results: dict[str, dict] = {}
    usage = {"out": 0, "reread": 0, "first": None, "last": None, "session": None, "agent": None, "model": None}
    with open(path, errors="ignore") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            m = o.get("message") or {}
            if prompt is None and agent_type != "root" and o.get("type") == "user" and not o.get("isMeta"):
                prompt = _result_text(m.get("content")) or None
            origin = _peer_origin(o.get("origin"))
            delivery = "new_turn"
            if origin is None and o.get("type") == "attachment" and isinstance(o.get("attachment"), dict):
                origin, delivery = _peer_origin(o["attachment"].get("origin")), "mid_turn"
            if origin is not None and origin.get("msg_id"):
                body = origin.get("body") or ""
                messages.append({"type": "message", "source": "claude", "msg_id": origin["msg_id"],
                                 "at": o.get("timestamp"), "receiver": o.get("sessionId"),
                                 "sender_name": origin.get("name"), "delivery": delivery,
                                 "first_line": redact(_first_line(body), 200), "body": redact(body, 1000),
                                 "body_chars": len(body)})
                continue
            if o.get("type") == "assistant" and isinstance(m.get("usage"), dict):
                u = m["usage"]
                usage["out"] += int(u.get("output_tokens") or 0)
                usage["reread"] += int(u.get("cache_read_input_tokens") or 0)
                usage["first"] = usage["first"] or o.get("timestamp")
                usage["last"] = o.get("timestamp") or usage["last"]
                usage["session"] = usage["session"] or o.get("sessionId")
                usage["agent"] = usage["agent"] or o.get("agentId")
                usage["model"] = usage["model"] or m.get("model")
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and o.get("type") == "assistant":
                    calls[b["id"]] = {"name": b.get("name"), "text": _text(b.get("input")),
                                      "ts": o.get("timestamp"), "session": o.get("sessionId"),
                                      "agent": o.get("agentId"), "model": m.get("model")}
                    order.append(b["id"])
                    if b.get("name") == "SendMessage" and isinstance(b.get("input"), dict):
                        sends[b["id"]] = b["input"]
                elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                    results[b["tool_use_id"]] = {"error": bool(b.get("is_error")),
                                                 "text": _result_text(b.get("content"))}
                    sent = sends.pop(b["tool_use_id"], None)
                    res = o.get("toolUseResult")
                    # Only a cross-session send gets a msg_id; a message to a sub-agent or
                    # teammate does not, which is what keeps those out.
                    if sent is not None and isinstance(res, dict) and res.get("msg_id"):
                        body = sent.get("message") or sent.get("content") or ""
                        body = body if isinstance(body, str) else ""
                        messages.append({
                            "type": "message", "source": "claude", "msg_id": res["msg_id"],
                            "at": calls.get(b["tool_use_id"], {}).get("ts") or o.get("timestamp"),
                            "sender": o.get("sessionId"), "addressed_to": sent.get("to"),
                            "first_line": redact(_first_line(body), 200), "body": redact(body, 1000),
                            "body_chars": len(body),
                            "notify_when_idle": bool(sent.get("notify_when_idle")),
                            "send_result": redact(str(res.get("message") or ""), 200) or None})
    out = []
    for cid in order:
        c, r = calls[cid], results.get(cid)
        if r is None or not c.get("ts"):
            continue
        ev = {"type": "event", "source": "claude", "session": c["session"] or os.path.basename(path),
              "ts": c["ts"], "surface": c["name"] or "?", "text": redact(c["text"], 1500), "ok": not r["error"],
              "actor": agent_type, "invocation": c["agent"] if agent_type != "root" else None,
              "model": c["model"]}
        if r["error"]:
            ev["error"] = redact(r["text"], 400)
        out.append(ev)
    if agent_type != "root" and usage["agent"] and usage["first"]:
        # Per-agent tokens straight from the agent's own transcript: the thing a
        # hook-based recorder once got wrong by reading the parent's running total.
        out.append({"type": "invocation", "source": "claude", "invocation": usage["agent"],
                    "session": usage["session"], "agent_type": agent_type, "started": usage["first"],
                    "ended": usage["last"], "status": "completed", "model": usage["model"],
                    "tokens_out": usage["out"], "tokens_reread": usage["reread"], "token_verified": True,
                    "tools": dict(Counter(e["surface"] for e in out)),
                    "description": meta.get("description"), "parent_call": meta.get("toolUseId"),
                    "workflow_run": workflow_run, "workflow_phase": meta.get("workflowPhase"),
                    "prompt": redact(prompt, 300) if prompt else None})
    return out + messages


def parse_workflow(path: str) -> dict | None:
    """`<session>/workflows/<runId>.json`: the record the Workflow tool writes when a run
    progresses and when it ends. Only the fields RunTune reasons over are kept: the
    script, arguments and result stay on disk."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (ValueError, OSError):
        return None
    if not isinstance(d, dict) or not d.get("runId"):
        return None
    started = d.get("startTime")
    if isinstance(started, (int, float)):
        from datetime import datetime, timezone
        started = datetime.fromtimestamp(started / 1000, tz=timezone.utc).isoformat()
    return {"type": "workflow", "source": "claude", "run_id": d["runId"],
            "session": os.path.basename(os.path.dirname(os.path.dirname(path))),
            "name": d.get("workflowName"), "status": d.get("status"),
            "started": started or d.get("timestamp"), "duration_ms": d.get("durationMs"),
            "agents_declared": d.get("agentCount"), "tokens_declared": d.get("totalTokens")}


def ingest(root: str = "~/.claude/projects") -> dict:
    state_path = home.path("recorder-state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    seen = state.setdefault("claude", {})
    files = glob.glob(os.path.join(os.path.expanduser(root), "**", "*.jsonl"), recursive=True)
    out_dir = home.path("events")
    os.makedirs(out_dir, exist_ok=True)
    n_files = n_events = 0
    for p in sorted(files):
        st = os.stat(p)
        sig = [st.st_size, int(st.st_mtime)]
        if seen.get(p) == sig:
            continue
        recs = parse(p)
        # Slice by EVENT count only. The summary records (an agent's invocation, its
        # messages) sit after the events and are rewritten as the file grows, so counting
        # them skipped one real event per summary record on the next read. They are
        # re-emitted every time instead; the loader keeps the latest of each.
        evs = [r for r in recs if r["type"] == "event"]
        new = evs[seen.get(p + "#n", 0):] + [r for r in recs if r["type"] != "event"]
        for ev in new:
            day = (ev.get("ts") or ev.get("started") or ev.get("at") or "")[:10]
            with open(os.path.join(out_dir, f"{day}.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")
        seen[p], seen[p + "#n"] = sig, len(evs)
        n_files += 1
        n_events += len(new)
    # Workflow run records are rewritten while a run progresses; the last version wins.
    runs = {}
    for p in sorted(glob.glob(os.path.join(os.path.expanduser(root), "*", "*", "workflows", "wf_*.json"))):
        st = os.stat(p)
        sig = [st.st_size, int(st.st_mtime)]
        if seen.get(p) == sig:
            continue
        rec = parse_workflow(p)
        if rec:
            runs[p] = rec
        seen[p] = sig
    for rec in runs.values():
        day = (rec.get("started") or "")[:10] or "undated"
        with open(os.path.join(out_dir, f"{day}.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        n_events += 1
    os.makedirs(home.root(), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)
    return {"files_read": n_files, "events_written": n_events, "files_seen": len(files)}
