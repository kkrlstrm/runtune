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

Three newer cc-logger additions are read when the warehouse has them, and their
absence is a coverage note, never an error (an engineer's older logger has none):

  * sub-agent provenance on `agent_invocations` (prompt_received, description,
    workflow_run_id, spawned_by_tool_call_id, source): what each agent was asked to
    do, so fan-outs group by task instead of by toolset;
  * `workflow_runs`: one row per Workflow fan-out run;
  * `vw_peer_messages`: cross-session messages, joined from both ends by msg_id.
"""

from __future__ import annotations

from collections import Counter

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Coverage, Event, Invocation, Message, WorkflowRun
from . import pg

_EVENTS_SQL = """
SELECT t.tool_call_id, t.session_id, t.invocation_id, t.tool_name,
       coalesce(t.subagent_type, 'root') AS actor, t.status,
       left(coalesce(t.tool_input->>'command', t.tool_input->>'skill',
                     t.tool_input->>'url', t.tool_input->>'file_path',
                     t.tool_input->>'description', t.tool_input->>'to', ''), 1500) AS text,
       left(coalesce(t.error, ''), 400) AS error,
       t.duration_ms, coalesce(t.started_at, t.received_at) AS ts, s.model
FROM tool_calls t LEFT JOIN sessions s USING (session_id)
WHERE coalesce(t.started_at, t.received_at) >= now() - make_interval(days => %s)
"""

_INVOCATIONS_SQL = """
SELECT a.invocation_id, a.session_id, a.agent_type, a.model, a.status,
       a.started_at, a.ended_at, a.output_tokens, a.cache_read_tokens, a.token_source{extra}
FROM agent_invocations a
WHERE a.agent_type IS NOT NULL AND a.started_at >= now() - make_interval(days => %s)
"""

# Provenance columns (cc-logger migration 006), each read only when present.
_PROVENANCE = {
    "description": "a.description",
    "prompt_received": "left(a.prompt_received, 600) AS prompt",
    "spawned_by_tool_call_id": "a.spawned_by_tool_call_id AS parent_call",
    "workflow_run_id": "a.workflow_run_id",
    "workflow_phase": "a.workflow_phase",
    "last_message": "length(a.last_message) AS result_chars",
    "source": "a.source AS row_source",
}

_WORKFLOWS_SQL = """
SELECT run_id, session_id, workflow_name, status, started_at, duration_ms, agent_count, total_tokens
FROM workflow_runs WHERE coalesce(started_at, recorded_at) >= now() - make_interval(days => %s)
"""

_MESSAGES_SQL = """
SELECT msg_id, at, sender_session_id, sender_inferred, sender_name, receiver_session_id,
       addressed_to, delivery, notify_when_idle, left(coalesce(body, summary, ''), 2000) AS body,
       body_chars
FROM vw_peer_messages WHERE at >= now() - make_interval(days => %s)
"""

# Tools a newer cc-logger captures; trusted only when the table its migration created exists.
_CAPTURE_BASE = {"Agent", "Bash", "Edit", "Write", "Read", "Skill", "WebFetch", "WebSearch"}


def load(dsn: str, days: int = 120) -> Corpus:
    rows = pg.query(dsn, _EVENTS_SQL, [days])
    # agent_invocations is the optional half: an older cc-logger has no token_source, and
    # some installs have no invocation table at all. Either way the tool-call evidence
    # stands; what is missing is said in the coverage notes.
    notes, inv_rows, wf_rows, msg_rows = [], [], [], []
    capture = set(_CAPTURE_BASE)
    cols = pg.columns(dsn, "agent_invocations")
    if not cols:
        notes.append("no agent_invocations table: no sub-agent census or token figures")
    else:
        sql = _INVOCATIONS_SQL
        if "token_source" not in cols:
            sql = sql.replace("a.token_source", "NULL::text AS token_source")
            notes.append("agent_invocations has no token_source column (an older cc-logger): "
                         "sub-agent token figures are not used")
        have = [c for c in _PROVENANCE if c in cols]
        sql = sql.replace("{extra}", "".join(f", {_PROVENANCE[c]}" for c in have))
        if "description" not in cols or "workflow_run_id" not in cols:
            notes.append("agent_invocations has no sub-agent provenance (cc-logger before migration 006): "
                         "fan-outs are grouped by toolset, not by task")
        inv_rows = pg.query(dsn, sql, [days])
    if pg.columns(dsn, "workflow_runs"):
        wf_rows = pg.query(dsn, _WORKFLOWS_SQL, [days])
        capture.add("Workflow")
    else:
        notes.append("no workflow_runs table: Workflow fan-outs are not recorded as runs")
    if pg.columns(dsn, "vw_peer_messages"):
        msg_rows = pg.query(dsn, _MESSAGES_SQL, [days])
        capture |= {"SendMessage", "ListAgents"}
    else:
        notes.append("no vw_peer_messages view: cross-session messages are not recorded")
    corpus = from_rows(rows, inv_rows, wf_rows, msg_rows, capture=capture)
    corpus.coverage["claude"].notes.extend(notes)
    return corpus


def from_rows(rows, inv_rows=(), wf_rows=(), msg_rows=(), capture=None) -> Corpus:
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
    runs = {}
    for r in wf_rows:
        run = WorkflowRun(
            source="claude", run_id=r["run_id"], session=r["session_id"], name=r.get("workflow_name"),
            status=r.get("status"), started=r.get("started_at"), duration_ms=r.get("duration_ms"),
            agents_declared=r.get("agent_count"), tokens_declared=r.get("total_tokens"))
        runs[run.run_id] = run
        corpus.workflow_runs.append(run)
    for r in inv_rows:
        verified = r.get("token_source") == "transcript-verified"
        wf = r.get("workflow_run_id")
        corpus.invocations.append(Invocation(
            source="claude", invocation=r["invocation_id"], session=r["session_id"],
            agent_type=r["agent_type"], started=r.get("started_at"), ended=r.get("ended_at"),
            status=r.get("status") or "?", model=r.get("model"),
            tokens_out=int(r.get("output_tokens") or 0) if verified else 0,
            tokens_reread=int(r.get("cache_read_tokens") or 0) if verified else 0,
            token_verified=verified,
            tools=per_inv_tools.get(r["invocation_id"], Counter()),
            failures=per_inv_fail.get(r["invocation_id"], 0),
            description=r.get("description"),
            prompt=redact(r["prompt"], 300) if r.get("prompt") else None,
            parent_call=r.get("parent_call"), workflow_run=wf,
            workflow_name=runs[wf].name if wf in runs else None, workflow_phase=r.get("workflow_phase"),
            result_chars=r.get("result_chars"), recovered=r.get("row_source") == "backfill",
        ))
    for r in msg_rows:
        body = r.get("body") or ""
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        corpus.messages.append(Message(
            source="claude", msg_id=r["msg_id"], at=r["at"], sender=r.get("sender_session_id"),
            receiver=r.get("receiver_session_id"), addressed_to=r.get("addressed_to"),
            sender_name=r.get("sender_name"), first_line=redact(first, 200), body=redact(body, 1000),
            body_chars=int(r.get("body_chars") or len(body)), delivery=r.get("delivery"),
            notify_when_idle=bool(r.get("notify_when_idle")),
            sender_inferred=bool(r.get("sender_inferred"))))
    cov.notes.append("capture is cc-logger's tool allowlist, not every call")
    corpus.capture_allowlist = {"claude": set(capture or _CAPTURE_BASE)}
    corpus.coverage["claude"] = cov
    return corpus.finalize()


def _tool_family(tool: str) -> str:
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"mcp__{parts[1]}" if len(parts) > 1 else tool
    return tool
