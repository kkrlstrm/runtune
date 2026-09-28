# Changelog

## Unreleased

- **Antigravity is a host.** `runtune record antigravity` backfills from the session
  transcripts under `~/.gemini/antigravity/brain`, incrementally. Its `run_command` is a shell
  surface everywhere shell evidence is read, so its failures can become constraints. The model
  is taken from the transcript's model-selection lines and left unknown before the first one.
  The warehouse source reads `antigravity_*` tables when a warehouse has them; none writes
  them yet, so it reports a coverage note.
- **Cursor is a host.** `runtune record cursor` backfills Cursor's agent history from its own
  store, read-only and incremental, with every outcome: failures, rejections, and shell exit
  codes (Cursor omits `exitCode` when it is 0; the recorder reports how many successes rest on
  that). The hook enforces in Cursor through `preToolUse` and answers in Cursor's format, and
  it recognises a Cursor payload even when Cursor reached it through `~/.claude/settings.json`,
  which Cursor imports by default. It does not record Cursor, because that import never
  delivers failures. `runtune schedule` reads the store before each digest. The warehouse
  source reads cursor-logger's `cursor_*` tables and is on by default; a warehouse without
  them gives a coverage note, not an error.

## 0.2.0 (2026-09-27)

- **Standalone.** RunTune has its own recording and enforcement hook (Claude Code + Codex),
  transcript backfill (`runtune record claude|codex`, with per-agent tokens) and an OpenRouter
  billing recorder. It no longer needs CallusGuard or a telemetry warehouse.
- **Local-first notifications.** A digest card, a desktop notification and `runtune reply`.
  Slack, email and plugin channels are optional extensions; `runtune schedule` handles
  launchd/cron. Fly deployment is optional and applies approvals as pull requests.
- **`runtune demo`** runs the whole loop on packaged synthetic data.
- **Corrections from acting on its own advice:**
  - clearance-day transitions;
  - benchmark callers;
  - bill-vs-log reconciliation for idle clearances;
  - every rule version is measured;
  - generated patterns match path-prefixed scripts, and a new correction-coverage gate;
  - skills cite commands as typed;
  - the reply reader is floored at send time;
  - recorder gaps are per source.

  See docs/EVIDENCE.md, Part 3.

## 0.1.0 (2026-09-27)

- First version: four derivers (constraints, capabilities, subagents, routes), admission gates,
  the promoter with its authority rules, controlled before/after measurement, the hash-chained
  ledger.
