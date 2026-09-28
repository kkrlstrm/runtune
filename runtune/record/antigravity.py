"""Backfill Antigravity history from its session transcripts (~/.gemini/antigravity/brain/**/transcript.jsonl).

Antigravity keeps transcripts under `~/.gemini/antigravity/brain/<session_id>/.system_generated/logs/transcript.jsonl`.

    MODEL calls -> tool_calls array: (name, args)
    GENERIC / ERROR_MESSAGE / tool results -> the outcome (ok, error, output)

Subagents run in their own session paths or invoke_subagent calls.
Files already read are remembered by (size, mtime) in recorder-state.json and only new events are appended.
"""

from __future__ import annotations

import glob
import json
import os
import re
from datetime import datetime, timezone

from .. import home
from ..evidence.redact import redact

_TEXT_KEYS = ("CommandLine", "command", "cmd", "AbsolutePath", "TargetFile", "file_path", "path", "url", "query", "pattern", "skill", "description")


def _clean_str(val) -> str:
    if not isinstance(val, str):
        return str(val or "")
    s = val.strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    return s


def _text(inp) -> str:
    if not isinstance(inp, dict):
        return _clean_str(inp)
    for k in _TEXT_KEYS:
        v = inp.get(k)
        if v:
            if isinstance(v, list):
                v = " ".join(map(str, v))
            return _clean_str(v)
    return ""


def _parse_exit_code(content: str) -> tuple[bool | None, str]:
    if not content:
        return None, ""
    if "The command exited with code 0" in content:
        return True, ""
    m = re.search(r"The command exited with code (-?\d+)", content)
    if m:
        code = int(m.group(1))
        return code == 0, f"exit {code}"
    if content.startswith("API error") or "UNAVAILABLE" in content or "Permission denied" in content:
        return False, content[:300]
    return True, ""


def parse(path: str) -> list[dict]:
    parts = path.split(os.sep)
    session_id = "unknown"
    if ".system_generated" in parts:
        idx = parts.index(".system_generated")
        if idx > 0:
            session_id = parts[idx - 1]
    else:
        session_id = os.path.basename(os.path.dirname(path))

    pending_calls: list[dict] = []
    events: list[dict] = []
    actor = "root"
    # the transcript names the model only when it changes; before that it is unknown, not guessed
    model = os.environ.get("RUNTUNE_MODEL") or None

    with open(path, errors="ignore") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue

            ts = o.get("created_at") or o.get("timestamp")
            source = o.get("source")
            stype = o.get("type")

            content = o.get("content") or ""
            if "Model Selection" in content:
                m_match = re.search(r"Model Selection` from \S+ to ([^.\n]+)", content)
                if m_match:
                    model = m_match.group(1).strip()

            tool_calls = o.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name") or "?"
                    args = tc.get("args") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            pass

                    sub_actor = actor
                    if name == "invoke_subagent":
                        subagents = args.get("Subagents") or []
                        if isinstance(subagents, list) and subagents:
                            sub_actor = subagents[0].get("TypeName") or subagents[0].get("Role") or "subagent"

                    text = _text(args)
                    pending_calls.append({
                        "name": name,
                        "text": text,
                        "ts": ts,
                        "model": model,
                        "actor": sub_actor,
                    })
            elif pending_calls and (stype in ("GENERIC", "ERROR_MESSAGE", "STEP_RESULT") or source in ("MODEL", "SYSTEM")):
                err_text = o.get("error") or ""
                ok = True
                err = ""
                if stype == "ERROR_MESSAGE" or err_text:
                    ok = False
                    err = err_text or content[:400]
                elif content:
                    parsed_ok, parsed_err = _parse_exit_code(content)
                    if parsed_ok is not None:
                        ok = parsed_ok
                        err = parsed_err

                call = pending_calls.pop(0)
                ev = {
                    "type": "event",
                    "source": "antigravity",
                    "session": session_id,
                    "ts": call["ts"] or ts or datetime.now(timezone.utc).isoformat(),
                    "surface": call["name"],
                    "text": redact(call["text"], 1500),
                    "ok": ok,
                    "actor": call["actor"],
                    "invocation": session_id if call["actor"] != "root" else None,
                    "model": call["model"],
                }
                if not ok:
                    ev["error"] = redact(err or "failed", 400)
                events.append(ev)

    return events


def default_brain_dir() -> str:
    return os.path.expanduser("~/.gemini/antigravity/brain")


def ingest(root: str | None = None) -> dict:
    root_dir = root or default_brain_dir()
    state_path = home.path("recorder-state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    seen = state.setdefault("antigravity", {})
    files = []
    expanded_root = os.path.expanduser(root_dir)
    for base, _, fns in os.walk(expanded_root):
        for fn in fns:
            if fn == "transcript.jsonl":
                files.append(os.path.join(base, fn))
    out_dir = home.path("events")
    os.makedirs(out_dir, exist_ok=True)
    n_files = n_events = 0
    for p in sorted(files):
        st = os.stat(p)
        sig = [st.st_size, int(st.st_mtime)]
        if seen.get(p) == sig:
            continue
        evs = parse(p)
        already = seen.get(p + "#n", 0)
        new = evs[already:]
        for ev in new:
            day = (ev.get("ts") or "")[:10]
            if not day:
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with open(os.path.join(out_dir, f"{day}.jsonl"), "a") as f:
                f.write(json.dumps(ev) + "\n")
        seen[p], seen[p + "#n"] = sig, len(evs)
        n_files += 1
        n_events += len(new)
    os.makedirs(home.root(), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)
    return {"files_read": n_files, "events_written": n_events, "files_seen": len(files)}
