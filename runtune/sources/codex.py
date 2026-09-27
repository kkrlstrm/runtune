"""Codex CLI, as recorded by codex-logger (rollout files -> codex_* tables).

Codex reports outcomes differently from Claude Code: a shell call is `exec` or
`exec_command`, its outcome is a `status` plus an `exit_code`, and a large share
of calls end `running` / `unknown` because the rollout closed first. Those are
unsettled and excluded from rates, exactly as Claude's pending/orphaned are.

`rejected` IS settled: the user (or the Codex guardian) refused the call. It is
counted as a failure of the attempt, because from the agent's side the thing it
tried did not happen — and a pattern that keeps getting rejected is precisely a
constraint candidate.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Coverage, Event, Invocation
from . import pg

_SETTLED = {"success", "failure", "rejected"}

_EVENTS_SQL = """
SELECT c.session_id, c.call_id, c.tool_name, c.status, c.exit_code,
       left(coalesce(c.command, c.arguments, ''), 1500) AS text,
       left(coalesce(c.error_text, CASE WHEN c.status <> 'success' THEN c.output END, ''), 400) AS error,
       c.duration_ms, c.ts, s.model, coalesce(s.subagent_type, 'root') AS actor
FROM codex_tool_calls c LEFT JOIN codex_sessions s USING (session_id)
WHERE c.ts >= now() - make_interval(days => %s)
"""

_SESSIONS_SQL = """
SELECT session_id, coalesce(subagent_type, 'root') AS agent_type, model, started_at, ended_at,
       num_tool_calls, output_tokens, cached_input_tokens, parent_thread_id
FROM codex_sessions WHERE started_at >= now() - make_interval(days => %s)
"""


def _command_text(raw: str) -> str:
    """Codex stores some calls' arguments as JSON ({"cmd": "..."} / {"command": [...]})."""
    s = raw.strip()
    if not s.startswith("{"):
        return raw
    try:
        obj = json.loads(s)
    except ValueError:
        m = re.search(r'"(?:cmd|command)"\s*:\s*"((?:[^"\\]|\\.)*)', s)
        return m.group(1).encode().decode("unicode_escape", "ignore") if m else raw
    cmd = obj.get("cmd") or obj.get("command") if isinstance(obj, dict) else None
    if isinstance(cmd, list):
        # ["bash", "-lc", "real command"] -> the real command
        return cmd[-1] if len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(map(str, cmd))
    return cmd if isinstance(cmd, str) else raw


def load(dsn: str, days: int = 120) -> Corpus:
    return from_rows(pg.query(dsn, _EVENTS_SQL, [days]), pg.query(dsn, _SESSIONS_SQL, [days]))


def from_rows(rows, session_rows=()) -> Corpus:
    corpus = Corpus()
    cov = Coverage("codex")
    tools: dict[str, Counter] = {}
    fails: Counter = Counter()
    for r in rows:
        status = r.get("status")
        tool = r.get("tool_name") or "?"
        tools.setdefault(r["session_id"], Counter())[tool] += 1
        if status not in _SETTLED:
            cov.unsettled += 1
            continue
        ok = status == "success" and (r.get("exit_code") in (None, 0))
        if not ok:
            fails[r["session_id"]] += 1
        text = _command_text(r.get("text") or "")
        corpus.events.append(Event(
            source="codex", session=r["session_id"], ts=r["ts"], kind="tool",
            surface=tool, shape=shapes.command_shape(tool, text), ok=ok,
            actor=r.get("actor") or "root", invocation=r["session_id"],
            text=redact(text, 800), chars=len(text),
            error="" if ok else shapes.normalize_error(
                r.get("error") or ("rejected by user/guardian" if status == "rejected" else f"exit {r.get('exit_code')}")),
            model=r.get("model"), duration_ms=r.get("duration_ms"),
            inline_target=shapes.inline_target(text),
        ))
    for r in session_rows:
        corpus.invocations.append(Invocation(
            source="codex", invocation=r["session_id"], session=r.get("parent_thread_id") or r["session_id"],
            agent_type=r.get("agent_type") or "root", started=r.get("started_at"), ended=r.get("ended_at"),
            status="completed" if r.get("ended_at") else "open", model=r.get("model"),
            # codex-logger reads tokens from the thread's own rollout: per-agent by construction
            tokens_out=int(r.get("output_tokens") or 0), tokens_reread=int(r.get("cached_input_tokens") or 0),
            token_verified=True, tools=tools.get(r["session_id"], Counter()),
            failures=fails.get(r["session_id"], 0),
        ))
    corpus.coverage["codex"] = cov
    return corpus.finalize()
