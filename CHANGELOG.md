# Changelog

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
