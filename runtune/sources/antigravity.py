"""Antigravity agent telemetry source (antigravity_tool_calls and antigravity_sessions).

Handles database warehouse queries for Antigravity traces. If the warehouse
does not contain antigravity_* tables, returns an empty corpus with a coverage note.
"""

from __future__ import annotations

from collections import Counter

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Coverage, Event, Invocation
from . import pg

_SETTLED = {"success", "failure", "rejected"}

_EVENTS_SQL = """
SELECT a.session_id, a.call_id, a.tool_name, a.status, a.exit_code,
       left(coalesce(a.command, a.target, a.arguments, ''), 1500) AS text,
       left(coalesce(a.error_text, ''), 400) AS error,
       a.ts, coalesce(a.model, s.model) AS model,
       coalesce(a.subagent_type, CASE WHEN s.parent_session_id IS NULL THEN 'root' ELSE 'subagent' END) AS actor,
       s.parent_session_id
FROM antigravity_tool_calls a LEFT JOIN antigravity_sessions s USING (session_id)
WHERE a.ts >= now() - make_interval(days => %s)
"""

_SESSIONS_SQL = """
SELECT session_id, parent_session_id, coalesce(models_seen, model) AS model,
       started_at, updated_at
FROM antigravity_sessions WHERE started_at >= now() - make_interval(days => %s)
"""


def load(dsn: str, days: int = 120) -> Corpus:
    try:
        rows = pg.query(dsn, _EVENTS_SQL, [days])
    except Exception as exc:  # noqa: BLE001
        if "antigravity_" in str(exc) and "does not exist" in str(exc):
            corpus = Corpus()
            corpus.coverage["antigravity"] = Coverage("antigravity", notes=["no antigravity_* tables in this warehouse"])
            return corpus
        raise
    return from_rows(rows, pg.query(dsn, _SESSIONS_SQL, [days]))


def from_rows(rows, session_rows=()) -> Corpus:
    corpus = Corpus()
    cov = Coverage("antigravity")
    tools: dict[str, Counter] = {}
    fails: Counter = Counter()
    for r in rows:
        surface = r.get("tool_name") or "?"
        tools.setdefault(r["session_id"], Counter())[surface] += 1
        status = r.get("status")
        if status not in _SETTLED:
            cov.unsettled += 1
            continue
        ok = status == "success"
        if not ok:
            fails[r["session_id"]] += 1
        text = r.get("text") or ""
        corpus.events.append(Event(
            source="antigravity", session=r.get("parent_session_id") or r["session_id"], ts=r["ts"],
            kind="tool", surface=surface, shape=shapes.command_shape(surface, text), ok=ok,
            actor=r.get("actor") or "root", invocation=r["session_id"],
            text=redact(text, 800), chars=len(text),
            error="" if ok else shapes.normalize_error(
                r.get("error") or ("rejected by user" if status == "rejected"
                                   else f"exit {r.get('exit_code')}")),
            model=r.get("model"), inline_target=shapes.inline_target(text),
        ))
    for r in session_rows:
        corpus.invocations.append(Invocation(
            source="antigravity", invocation=r["session_id"],
            session=r.get("parent_session_id") or r["session_id"],
            agent_type="subagent" if r.get("parent_session_id") else "root",
            started=r.get("started_at"), ended=r.get("updated_at"), status="completed",
            model=r.get("model"), tools=tools.get(r["session_id"], Counter()),
            failures=fails.get(r["session_id"], 0),
        ))
    corpus.coverage["antigravity"] = cov
    return corpus.finalize()
