# Warehouse sources: the column contract

You don't need a warehouse: `runtune record claude|codex|cursor` and the hook write local JSONL.
If you already store agent telemetry in Postgres, point `--db` (or `$RUNTUNE_DB_URL`) at it
and RunTune reads these tables in a read-only session. Give it a SELECT-only role.

A table or column a source needs and the warehouse lacks turns that source into a coverage
note and a warning; the other sources still load. `agent_invocations` is optional, and so is
its `token_source` column (an older cc-logger has neither): the tool-call evidence is read
either way, and the report says that sub-agent token figures were not used.

## Claude Code (cc-logger schema, as recorded by CallusGuard's recorder)

| table | columns read |
|---|---|
| `tool_calls` | tool_call_id, session_id, invocation_id, tool_name, subagent_type, tool_input (jsonb: command/skill/url/file_path), status (`success`/`failure`/`pending`/`orphaned`), error, duration_ms, started_at, received_at |
| `sessions` | session_id, model |
| `agent_invocations` | invocation_id, session_id, agent_type, model, status, started_at, ended_at, output_tokens, cache_read_tokens, token_source (only `transcript-verified` rows' tokens are used); optional: description, prompt_received, spawned_by_tool_call_id, workflow_run_id, workflow_phase, last_message (length only), source |
| `workflow_runs` (optional) | run_id, session_id, workflow_name, status, started_at, recorded_at, duration_ms, agent_count, total_tokens |
| `vw_peer_messages` (optional) | msg_id, at, sender_session_id, sender_inferred, sender_name, receiver_session_id, addressed_to, delivery, notify_when_idle, body, summary, body_chars |

The three optional parts are cc-logger's migrations 005 and 006 (2026-10-13). With them, a
fan-out is named by what its agents were asked to do and grouped by Workflow run, and
cross-session messages feed the coordination findings. Without them, each is a coverage note.
`SendMessage`, `ListAgents` and `Workflow` count as captured tools only when the table that
records them exists, so an older logger never reports them as unused.

## Codex (codex-logger schema)

| table | columns read |
|---|---|
| `codex_tool_calls` | session_id, call_id, tool_name, status (`success`/`failure`/`rejected`, others unsettled), exit_code, command, arguments, output, error_text, duration_ms, ts |
| `codex_sessions` | session_id, subagent_type, model, started_at, ended_at, output_tokens, cached_input_tokens, parent_thread_id |

## Cursor (cursor-logger schema)

| table | columns read |
|---|---|
| `cursor_tool_calls` | session_id, call_id, tool_name (`run_terminal_command_v2`/`run_terminal_cmd` read as `shell`), status (`success`/`failure`/`rejected`, others unsettled), exit_code, outcome_basis, command, target, arguments, error_text, model, ts |
| `cursor_sessions` | session_id, parent_session_id, model, models_seen, started_at, updated_at |

Cursor keeps no billed token usage locally, so Cursor invocations carry no token figures. A
warehouse without these tables gives a coverage note, not an error, so `cursor` is in the default
source list.

## OpenRouter

Table names are configurable with `$RUNTUNE_OR_CALLS_TABLE` (default `openrouter_calls`) and
`$RUNTUNE_OR_ACTIVITY_TABLE` (default `openrouter_activity`); `schema.table` is accepted.

| table | columns read |
|---|---|
| calls: one row per request from your router | call_id, ts, mode, model, usd, tok_in, tok_out, ok, route, caller_file, caller_func, entrypoint, label, duration_ms, host |
| activity: the provider's per-day bill (`GET /api/v1/activity`) | usage_date, model, provider_name, requests, prompt_tokens, completion_tokens, reasoning_tokens, usage_usd |

The allowlist is a JSON file passed with `--routes`:

```json
{"staleness_warn_days": 120,
 "modes": {"extract": {"model": "deepseek/deepseek-v4-flash", "verified_date": "2026-06-04"}},
 "retired": {}}
```

The route deriver joins these three: what was approved, what ran, and what was billed. Tag
benchmark and eval traffic with a caller or entrypoint name containing `bench`, `eval` or
`experiment`, so it is kept out of the production findings.
