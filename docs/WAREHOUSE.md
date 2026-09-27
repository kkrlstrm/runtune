# Warehouse sources: the column contract

You don't need a warehouse: `runtune record claude|codex` and the hook write local JSONL.
If you already store agent telemetry in Postgres, point `--db` (or `$RUNTUNE_DB_URL`) at it
and RunTune reads these tables in a read-only session. Give it a SELECT-only role.

## Claude Code (cc-logger schema, as recorded by CallusGuard's recorder)

| table | columns read |
|---|---|
| `tool_calls` | tool_call_id, session_id, invocation_id, tool_name, subagent_type, tool_input (jsonb: command/skill/url/file_path), status (`success`/`failure`/`pending`/`orphaned`), error, duration_ms, started_at, received_at |
| `sessions` | session_id, model |
| `agent_invocations` | invocation_id, session_id, agent_type, model, status, started_at, ended_at, output_tokens, cache_read_tokens, token_source (only `transcript-verified` rows' tokens are used) |

## Codex (codex-logger schema)

| table | columns read |
|---|---|
| `codex_tool_calls` | session_id, call_id, tool_name, status (`success`/`failure`/`rejected`, others unsettled), exit_code, command, arguments, output, error_text, duration_ms, ts |
| `codex_sessions` | session_id, subagent_type, model, started_at, ended_at, output_tokens, cached_input_tokens, parent_thread_id |

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
