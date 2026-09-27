"""Backfill Claude Code history from its own transcripts (~/.claude/projects/**.jsonl).

The hook records from the day it is installed; this reads what Claude Code already
kept, so a new install has evidence on day one. Claude Code deletes transcripts
after `cleanupPeriodDays` (30 by default), so the backfill reaches back that far.

    assistant record  -> tool_use blocks: the call (id, name, input) and the model
    user record       -> tool_result blocks: the outcome (is_error, content)
    subagents/agent-<id>.jsonl + .meta.json  -> the same, with the sub-agent's type

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


def parse(path: str) -> list[dict]:
    agent_type = "root"
    meta = path[:-len(".jsonl")] + ".meta.json"
    if os.path.exists(meta):
        try:
            with open(meta) as f:
                agent_type = json.load(f).get("agentType") or "subagent"
        except (ValueError, OSError):
            agent_type = "subagent"
    elif "/subagents/" in path:
        agent_type = "workflow-subagent" if "/workflows/" in path else "subagent"
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
                elif b.get("type") == "tool_result" and b.get("tool_use_id"):
                    results[b["tool_use_id"]] = {"error": bool(b.get("is_error")),
                                                 "text": _result_text(b.get("content"))}
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
                    "tools": dict(Counter(e["surface"] for e in out))})
    return out


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
        evs = parse(p)
        new = evs[seen.get(p + "#n", 0):]
        for ev in new:
            day = (ev.get("ts") or ev.get("started") or "")[:10]
            with open(os.path.join(out_dir, f"{day}.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")
        seen[p], seen[p + "#n"] = sig, len(evs)
        n_files += 1
        n_events += len(new)
    os.makedirs(home.root(), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)
    return {"files_read": n_files, "events_written": n_events, "files_seen": len(files)}
