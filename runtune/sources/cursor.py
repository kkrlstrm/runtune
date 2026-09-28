"""Cursor's agent, as recorded by cursor-logger (Cursor's state.vscdb -> cursor_* tables).

Tool names are mapped exactly as `runtune record cursor` maps them (`record.cursor.surface`):
the two terminal tools become the `shell` surface, so a Cursor command is shaped and
constrained like a Claude `Bash` call or a Codex `exec_command`, and other tools lose a
trailing `_v2`. The warehouse and the local recorder therefore cluster the same calls
under the same keys.

A warehouse with no cursor_* tables (cursor-logger not installed there) yields an empty
corpus and a coverage note, so `cursor` can sit in the default source list.

Settled = success / failure / rejected, as for Codex. `aborted` (cancelled) and
`error` / `unknown` are unsettled and stay out of rates.

A shell success can rest on an inferred exit code: Cursor omits `exitCode` when it is 0,
and cursor-logger records that case as outcome_basis='exit_code_omitted'. It is settled
here, and the Coverage note counts how many settled attempts rest on it.

Tokens: Cursor does not store billed usage locally. `context_tokens_used` is the size of
the context window at the session's last request, not tokens re-read per turn, so
invocations carry no token figures and token_verified stays False.
"""

from __future__ import annotations

from collections import Counter

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Coverage, Event, Invocation
from ..record.cursor import surface as _surface
from . import pg

_SETTLED = {"success", "failure", "rejected"}

_EVENTS_SQL = """
SELECT c.session_id, c.call_id, c.tool_name, c.status, c.exit_code, c.outcome_basis,
       left(coalesce(c.command, c.target, c.arguments, ''), 1500) AS text,
       left(coalesce(c.error_text, ''), 400) AS error,
       c.ts, coalesce(c.model, s.model) AS model,
       CASE WHEN s.parent_session_id IS NULL THEN 'root' ELSE 'subagent' END AS actor,
       s.parent_session_id
FROM cursor_tool_calls c LEFT JOIN cursor_sessions s USING (session_id)
WHERE c.ts >= now() - make_interval(days => %s)
"""

_SESSIONS_SQL = """
SELECT session_id, parent_session_id, coalesce(models_seen, model) AS model,
       started_at, updated_at
FROM cursor_sessions WHERE started_at >= now() - make_interval(days => %s)
"""


def load(dsn: str, days: int = 120) -> Corpus:
    try:
        rows = pg.query(dsn, _EVENTS_SQL, [days])
    except Exception as exc:  # noqa: BLE001 - psycopg is an optional import
        if "cursor_" in str(exc) and "does not exist" in str(exc):
            corpus = Corpus()
            corpus.coverage["cursor"] = Coverage("cursor", notes=["no cursor_* tables in this warehouse"])
            return corpus
        raise
    return from_rows(rows, pg.query(dsn, _SESSIONS_SQL, [days]))


def from_rows(rows, session_rows=()) -> Corpus:
    corpus = Corpus()
    cov = Coverage("cursor")
    tools: dict[str, Counter] = {}
    fails: Counter = Counter()
    inferred = 0
    for r in rows:
        surface = _surface(r.get("tool_name"))
        tools.setdefault(r["session_id"], Counter())[surface] += 1
        status = r.get("status")
        if status not in _SETTLED:
            cov.unsettled += 1
            continue
        ok = status == "success"
        if not ok:
            fails[r["session_id"]] += 1
        if r.get("outcome_basis") == "exit_code_omitted":
            inferred += 1
        text = r.get("text") or ""
        corpus.events.append(Event(
            source="cursor", session=r.get("parent_session_id") or r["session_id"], ts=r["ts"],
            kind="tool", surface=surface, shape=shapes.command_shape(surface, text), ok=ok,
            actor=r.get("actor") or "root", invocation=r["session_id"],
            text=redact(text, 800), chars=len(text),
            error="" if ok else shapes.normalize_error(
                r.get("error") or ("rejected by user" if status == "rejected"
                                   else f"exit {r.get('exit_code')}")),
            model=r.get("model"), inline_target=shapes.inline_target(text),
        ))
    if inferred:
        cov.notes.append(f"{inferred:,} shell successes rest on an omitted exit code (Cursor drops exitCode=0)")
    for r in session_rows:
        corpus.invocations.append(Invocation(
            source="cursor", invocation=r["session_id"],
            session=r.get("parent_session_id") or r["session_id"],
            agent_type="subagent" if r.get("parent_session_id") else "root",
            started=r.get("started_at"), ended=r.get("updated_at"), status="completed",
            model=r.get("model"), tools=tools.get(r["session_id"], Counter()),
            failures=fails.get(r["session_id"], 0),
        ))
    corpus.coverage["cursor"] = cov
    return corpus.finalize()
