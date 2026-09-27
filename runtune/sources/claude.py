"""Claude Code, as recorded by cc-logger (the callusguard recorder).

Reads `tool_calls`, `sessions` and `agent_invocations`. Three facts about that data
shape every choice below:

  * `status` is success | failure | pending | orphaned. Only the first two are
    settled; the rest are counted in coverage and excluded from every rate.
  * cc-logger's capture is an ALLOWLIST of tools (Agent, Bash, Edit, Write, Read,
    Skill, WebFetch, WebSearch, mcp__*), not every call. A tool outside it is
    invisible here, which is different from unused.
  * `agent_invocations` token columns were broadcast from the root transcript for
    most historical rows (inflated ~39x on cache reads). Only rows with
    token_source = 'transcript-verified' carry per-agent tokens, and RunTune uses
    tokens from no other row.
"""

from __future__ import annotations

from collections import Counter

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Coverage, Event, Invocation
from . import pg

_EVENTS_SQL = """
SELECT t.tool_call_id, t.session_id, t.invocation_id, t.tool_name,
       coalesce(t.subagent_type, 'root') AS actor, t.status,
       left(coalesce(t.tool_input->>'command', t.tool_input->>'skill',
                     t.tool_input->>'url', t.tool_input->>'file_path', ''), 1500) AS text,
       left(coalesce(t.error, ''), 400) AS error,
       t.duration_ms, coalesce(t.started_at, t.received_at) AS ts, s.model
FROM tool_calls t LEFT JOIN sessions s USING (session_id)
WHERE coalesce(t.started_at, t.received_at) >= now() - make_interval(days => %s)
"""

_INVOCATIONS_SQL = """
SELECT a.invocation_id, a.session_id, a.agent_type, a.model, a.status,
       a.started_at, a.ended_at, a.output_tokens, a.cache_read_tokens, a.token_source
FROM agent_invocations a
WHERE a.agent_type IS NOT NULL AND a.started_at >= now() - make_interval(days => %s)
"""


def load(dsn: str, days: int = 120) -> Corpus:
    rows = pg.query(dsn, _EVENTS_SQL, [days])
    return from_rows(rows, pg.query(dsn, _INVOCATIONS_SQL, [days]))


def from_rows(rows, inv_rows=()) -> Corpus:
    corpus = Corpus()
    cov = Coverage("claude")
    per_inv_tools: dict[str, Counter] = {}
    per_inv_fail: Counter = Counter()
    for r in rows:
        status = r.get("status")
        tool = r.get("tool_name") or "?"
        inv = r.get("invocation_id")
        if inv:
            per_inv_tools.setdefault(inv, Counter())[_tool_family(tool)] += 1
        if status not in ("success", "failure"):
            cov.unsettled += 1
            continue
        text = r.get("text") or ""
        ok = status == "success"
        if not ok and inv:
            per_inv_fail[inv] += 1
        corpus.events.append(Event(
            source="claude", session=r["session_id"], ts=r["ts"], kind="tool",
            surface=tool, shape=shapes.command_shape(tool, text), ok=ok,
            actor=r.get("actor") or "root", invocation=inv,
            text=redact(text, 800), chars=len(text),
            error="" if ok else shapes.normalize_error(r.get("error")),
            model=r.get("model"), duration_ms=r.get("duration_ms"),
            inline_target=shapes.inline_target(text) if tool == "Bash" else None,
        ))
    for r in inv_rows:
        verified = r.get("token_source") == "transcript-verified"
        corpus.invocations.append(Invocation(
            source="claude", invocation=r["invocation_id"], session=r["session_id"],
            agent_type=r["agent_type"], started=r.get("started_at"), ended=r.get("ended_at"),
            status=r.get("status") or "?", model=r.get("model"),
            tokens_out=int(r.get("output_tokens") or 0) if verified else 0,
            tokens_reread=int(r.get("cache_read_tokens") or 0) if verified else 0,
            token_verified=verified,
            tools=per_inv_tools.get(r["invocation_id"], Counter()),
            failures=per_inv_fail.get(r["invocation_id"], 0),
        ))
    cov.notes.append("capture is cc-logger's tool allowlist, not every call")
    corpus.capture_allowlist = {"claude": {"Agent", "Bash", "Edit", "Write", "Read", "Skill", "WebFetch", "WebSearch"}}
    corpus.coverage["claude"] = cov
    return corpus.finalize()


def _tool_family(tool: str) -> str:
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"mcp__{parts[1]}" if len(parts) > 1 else tool
    return tool
