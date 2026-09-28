"""Backfill Cursor's agent history from Cursor's own store (state.vscdb) into RunTune events.

Cursor keeps every agent conversation in one SQLite file, table cursorDiskKV:

    composerData:<id>          one session: model setting, timestamps, subagent ids,
                               and the ordered message ids
    bubbleId:<id>:<bubble>     one message; an assistant message may carry one tool
                               call in `toolFormerData` (name, params, result, status, error)

It is Cursor's internal format (composerData._v 18 on Cursor 3.21), read read-only with
the standard library. A session that stops parsing after a Cursor update is skipped and
counted, never half-read.

Why this, and not the hook, records Cursor: Cursor runs Claude Code hooks from
~/.claude/settings.json by default, but maps only PreToolUse and PostToolUse, not
PostToolUseFailure. A hook-based Cursor recorder would see successes and miss failures,
and its failure rate would read as zero. The store has every outcome.

Outcome, settled only when the store says so:
    completed                         success, except as below
    status error / result.error       failure, with the message the model was shown
    result.rejected                   failure ("rejected by user"), as Codex's rejected
    shell exit code != 0              failure, with the tail of the output
    cancelled / no status             unsettled: skipped, as Claude's pending
A shell call's exit code is `exitCodeV2`, else `exitCode`. Cursor writes the older
`exitCode` only when it is nonzero (protobuf drops defaults), so a completed shell call
with no exit code is exit 0. `ingest` reports how many successes rest on that.

Cursor's two terminal tools are recorded as the `shell` surface, so their commands are
shaped and constrained like Claude's Bash and Codex's exec. Other tools keep their
Cursor names, less a trailing `_v2`.

Cursor keeps no billed token usage locally, so no invocation token figures are written.
Calls already written are remembered per session by call id: a restored checkpoint
removes calls from the store, but those calls did run, and their events stay.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from .. import home
from ..evidence.redact import redact

SHELL_TOOLS = {"run_terminal_command_v2", "run_terminal_cmd"}
_TARGET_KEYS = ("command", "targetFile", "relativeWorkspacePath", "path", "targetDirectory",
                "globPattern", "pattern", "url", "searchTerm", "query")


def default_store() -> str:
    if sys.platform == "darwin":
        base = "~/Library/Application Support/Cursor"
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", "~"), "Cursor")
    else:
        base = os.path.join(os.environ.get("XDG_CONFIG_HOME", "~/.config"), "Cursor")
    return os.path.expanduser(os.path.join(base, "User", "globalStorage", "state.vscdb"))


def surface(tool: str | None) -> str:
    if tool in SHELL_TOOLS:
        return "shell"
    t = tool or "?"
    return t[:-3] if t.endswith("_v2") else t


def _loads(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _code(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _error_text(tf: dict, result) -> str:
    e = _loads(tf.get("error"))
    if isinstance(e, dict):
        msg = e.get("modelVisibleErrorMessage") or e.get("clientVisibleErrorMessage")
        if msg:
            return str(msg).strip()
    r = result.get("error") if isinstance(result, dict) else None
    while isinstance(r, dict):
        r = r.get("error") or r.get("message")
    return str(r).strip() if r else ""


def outcome(tf: dict) -> tuple[bool | None, str, bool]:
    """-> (ok or None when unsettled, error text, exit code inferred)."""
    st = tf.get("status")
    result = _loads(tf.get("result"))
    if st == "error":
        return False, _error_text(tf, result) or "tool reported error", False
    if st != "completed":
        return None, "", False
    if isinstance(result, dict):
        if result.get("rejected") is True:
            return False, "rejected by user", False
        if tf.get("name") in SHELL_TOOLS:
            if str(result.get("endedReason") or "").endswith("ABORTED"):
                return None, "", False
            code = _code(result.get("exitCodeV2"))
            if code is None:
                code = _code(result.get("exitCode"))
            if code is None:
                return True, "", True
            if code:
                out = [ln for ln in str(result.get("output") or "").splitlines() if ln.strip()]
                return False, "\n".join(out[-4:]) or f"exit {code}", False
            return True, "", False
        if result.get("error"):
            return False, _error_text(tf, result), False
    return True, "", False


def _text(tf: dict) -> str:
    params = _loads(tf.get("params"))
    if not isinstance(params, dict):
        return ""
    for k in _TARGET_KEYS:
        v = params.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _iso_ms(ms) -> str | None:
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    return None


def connect(path: str) -> sqlite3.Connection:
    """Read-only: Cursor holds the file open in WAL mode and keeps writing to it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def composers(con: sqlite3.Connection) -> list[dict]:
    out = []
    for (v,) in con.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
        m = _loads(v)
        if isinstance(m, dict) and m.get("composerId") and m.get("fullConversationHeadersOnly"):
            out.append(m)
    return out


def _bubbles(con: sqlite3.Connection, cid: str) -> dict:
    rows = con.execute("SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
                       (f"bubbleId:{cid}:", f"bubbleId:{cid};"))
    out = {}
    for k, v in rows:
        b = _loads(v)
        if isinstance(b, dict):
            out[k.rsplit(":", 1)[1]] = b
    return out


def signature(con: sqlite3.Connection, meta: dict) -> str:
    """lastUpdatedAt + bubble count: an index-only count, so an unchanged session is
    recognised without reading its messages."""
    cid = meta["composerId"]
    n = con.execute("SELECT count(*) FROM cursorDiskKV WHERE key >= ? AND key < ?",
                    (f"bubbleId:{cid}:", f"bubbleId:{cid};")).fetchone()[0]
    return f"{meta.get('lastUpdatedAt')}:{n}"


def parse_session(con: sqlite3.Connection, meta: dict, parent: str | None = None) -> tuple[list, int]:
    """-> ([(call_id, event dict, exit code inferred)], unsettled count)."""
    cid = meta["composerId"]
    bubbles = _bubbles(con, cid)
    order = [h.get("bubbleId") for h in meta.get("fullConversationHeadersOnly") or []
             if h.get("bubbleId") in bubbles]
    seen = set(order)
    order += sorted((k for k in bubbles if k not in seen), key=lambda k: str(bubbles[k].get("createdAt") or ""))
    model = (meta.get("modelConfig") or {}).get("modelName")
    ts = _iso_ms(meta.get("createdAt"))
    unsettled = 0
    out = []
    for bid in order:
        b = bubbles[bid]
        c = b.get("createdAt")
        ts = (c if isinstance(c, str) else _iso_ms(c)) or ts
        model = (b.get("modelInfo") or {}).get("modelName") or model
        tf = b.get("toolFormerData")
        # user messages can carry a stub with no tool name and no call id: nothing ran
        if not isinstance(tf, dict) or not (tf.get("name") or tf.get("toolCallId")):
            continue
        ok, err, inferred = outcome(tf)
        if ok is None or not ts:
            unsettled += 1
            continue
        ev = {"type": "event", "source": "cursor", "session": parent or cid, "ts": ts,
              "surface": surface(tf.get("name")), "text": redact(_text(tf), 1500), "ok": ok,
              "actor": "subagent" if parent else "root", "invocation": cid if parent else None,
              "model": model}
        if not ok:
            ev["error"] = redact(err, 400)
        out.append((tf.get("toolCallId") or bid, ev, inferred))
    return out, unsettled


def ingest(path: str | None = None) -> dict:
    path = os.path.expanduser(path or default_store())
    if not os.path.exists(path):
        return {"store": path, "error": "no Cursor store at this path", "events_written": 0}
    state_path = home.path("recorder-state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    seen = state.setdefault("cursor", {})
    out_dir = home.path("events")
    os.makedirs(out_dir, exist_ok=True)
    res = {"store": path, "sessions_seen": 0, "sessions_read": 0, "events_written": 0,
           "unsettled": 0, "exit_code_inferred": 0, "failed": 0}
    con = connect(path)
    try:
        metas = composers(con)
        parents = {child: m["composerId"] for m in metas
                   for child in (m.get("subagentComposerIds") or []) + (m.get("subComposerIds") or [])
                   if isinstance(child, str)}
        for meta in metas:
            cid = meta["composerId"]
            res["sessions_seen"] += 1
            st = seen.setdefault(cid, {"sig": None, "ids": []})
            sig = signature(con, meta)
            if st["sig"] == sig:
                continue
            try:
                evs, unsettled = parse_session(con, meta, parents.get(cid))
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
                res["failed"] += 1       # left unmarked, so the next run retries it
                print(f"runtune: cursor session {cid[:8]} did not parse: {exc!r}", file=sys.stderr)
                continue
            known = set(st["ids"])
            for call_id, ev, inferred in evs:
                if call_id in known:
                    continue
                with open(os.path.join(out_dir, f"{ev['ts'][:10]}.jsonl"), "a") as f:
                    f.write(json.dumps(ev) + "\n")
                st["ids"].append(call_id)
                res["events_written"] += 1
                res["exit_code_inferred"] += inferred
            res["unsettled"] += unsettled
            st["sig"] = sig
            res["sessions_read"] += 1
    finally:
        con.close()
    os.makedirs(home.root(), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(state, f)
    return res
