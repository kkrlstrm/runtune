# Changelog

## Unreleased

- **`runtune verify`: run a proposal before anyone applies it.** A staged capability or
  sub-agent runs headless on tasks you write, against control (the repository
  as it is) and treatment (the draft overlaid), in a contained copy of the repository:
  command shims, a fail-closed write hook, and the OS sandbox with `strictAllowlist`
  (`bypassPermissions` otherwise lets the sandbox reach any host). Verdicts are Fisher-tested,
  and the best reachable p is printed before any run is paid for. `--static` is free and
  catches a skill naming a command that does not exist, which RunTune once shipped.
  `--selftest` proves the containment on the current machine. `apply --eval` now validates a
  verify result (verdict, artifact, draft digest, target digest) instead of accepting any file
  for these kinds, and `require_verify` in `authority.json` makes one mandatory per kind.
  See `docs/VERIFY.md`.
- **Verify runs Codex, and has runners for Cursor and Antigravity.** The task file's `host`
  picks the agent; `--static` fails when that host would never load the artifact's target.
  Every non-Claude run gets its own home directory, so the user's MCP servers (whose
  credentials `~/.codex/config.toml` can hold in plaintext), plugins, hooks and skills stay
  out. Codex is contained by a Seatbelt permission profile (credential, `.env` and `.runtune`
  reads denied, the real repository read-only, network off) and verified end to end: on the
  demo repository the drafted skill took Codex to the CLI in 8 of 8 runs against 0 of 8
  without it (p < 0.001), with every answer correct. Cursor and
  Antigravity are built from their documented formats and are refused until `--selftest --host`
  passes on the installed CLI version, as are Codex upgrades. The selftest now runs any probe a
  model declines directly under the sandbox, without a model: Codex skipped every
  exfiltration-shaped step and replied "DONE".
- **A route's `--eval` must be about that route.** `apply` used to accept any file that
  existed, so `{}` admitted a model to the allowlist. A route eval is now a small result the
  eval harness writes (`runtune.route-eval/1`): it must name the same mode, candidate and
  incumbent, say `pass`, report both scores (a pass below the incumbent needs a declared
  non-inferiority margin), have scored at least `route_eval.min_cases` and be no older than
  `route_eval.max_age_days` (`authority.json`; 20 and 30 by default). Its sha256 goes into
  `routes.json` and the ledger. Re-clearing a stale mode now applies through the same check.
- **`verify --init` drafts tasks from real sessions.** Capability samples keep their session,
  and `--init` reads each sampled session back from the host's own transcript (Claude Code,
  Codex, Cursor, Antigravity) for requests that led the agent straight to the old path: the
  prompt, what the command printed and what the agent answered. Nothing is added to the event
  store. A draft is held (`"reviewed": false`) until a person has read it. On one real
  repository the filters left 4 drafts from 35 sessions: most inline calls happen deep inside
  longer work, and most direct requests are follow-ups.

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
