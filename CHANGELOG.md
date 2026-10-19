# Changelog

## Unreleased

**Standing: a skill or sub-agent is on trial until distinct sessions show it working.**
Review asks whether a rate moved against a control; standing asks, one session at a time,
whether the artifact worked when it was used. An applied capability or RunTune-written
sub-agent starts as `trial`. A use is a session after the change went live that exercised it
(the CLI the skill points to ran; the agent type was invoked), counted once by how its last use
ended. Three distinct clean sessions promote it to `proven` (ledger only). When the last two
sessions that used it ended badly, review proposes a **demotion**, which the digest offers
first: approving it retires the artifact and restores the revision it replaced, which is kept
as `superseded` when a revision is applied. Declining snoozes it. `runtune demote <id>` does
the same from a terminal. Thresholds: `standing` in authority.json. Constraints and routes
have no standing; a grant narrowing has none either. Adapted from @parcha/pi-agent-learn's
workflow promotion, with outcomes from recorded rows and a demotion that waits for a person.

**An approval interrupted between apply and publish is resumed, not lost.** `inbox --git`
applied every approved item and only then pushed; a failed push or a stopped machine left
artifacts `active` that no branch held, a server that re-syncs its clone each run then lost
them, and the reply was re-read with every item refused as "already active". Now:

- each approved item gets receipts (`.runtune/receipts/<digest>.json`): approved, applied,
  published (branch and commit), each written atomically after the step happened;
- the digest is saved as answered before the publish starts;
- an artifact applied but not yet published is marked `pending_publish`, and review reports
  it as pending instead of measuring a change nobody can see;
- the next `inbox --git` resumes it first: when the target was reset under it, the artifact
  goes back to staged and is applied again through every apply check (stale target, write
  guard), then published;
- a reply re-read after an interruption reports "already applied"; an apply interrupted
  before its receipt is recognised from the artifact's state;
- `publish` is idempotent: a digest branch `origin` already holds is reported, not pushed again.

**A write guard on everything RunTune writes.** Redaction already scrubbed what RunTune
stores; nothing checked what it writes. `stage` and `apply` now check the artifact (skill,
agent file, ruleset rule, grant, route, title and claim), and `inbox --git` checks the
staged diff and the pull-request body before it commits or pushes. A hit refuses; nothing is
redacted, because an artifact with a hole cut in it is not the one a person reviewed, and no
refusal repeats the value it matched. It refuses:

- a configured secret (environment variables named like credentials, plus `secret_env`) as
  written, split over lines, as base64 or base64url at any of the three alignments, as hex,
  and percent-encoded;
- token shapes (AWS, Google, GitHub, GitLab, `sk-` keys, Slack, JWTs, private keys, a URL
  with a password);
- invisible Unicode (zero-width, bidirectional, tag, private-use, controls), except joiners
  that are text: emoji sequences and letters of scripts that need them;
- case data: values from the host's own lists (`guard.case_values` in authority.json, e.g.
  client names from a registry file), matched as words across case, Unicode compatibility
  forms and line breaks. A skill that names a client is about a case, not the work.

A configured source that is missing refuses everything rather than checking nothing.
`runtune guard <files>` runs the same check from a shell (exit 4 on a refusal). Adapted from
@parcha/pi-agent-learn's guard (see docs/LINEAGE.md).

**Sub-agents, workflow runs and cross-session messages are evidence.** cc-logger now
records what each sub-agent was asked to do, every Workflow run, and both ends of each
message one Claude Code session sends another. RunTune reads all three from the warehouse,
and `runtune record claude` reads them from Claude Code's own files, so nothing needs a
warehouse.

- **Sub-agent findings name the task.** An invocation carries its description, prompt
  (redacted), exact parent call and workflow run. A fan-out is one Workflow run when there
  is one, and the finding says which workflows run their agents as the generic type, names
  the drafted type after the task, and says where to pass it (`agentType`). Without the new
  columns it groups by toolset, as before, and says so.
- **Coordination is a finding kind.** Two sessions deploying the same app or editing the
  same file inside a window, with no message between them, counted only from the day
  messaging first appears in the evidence; messages measured against the rules a reader
  needs (a checkable first line, who is sending, whether a reply is needed); sessions that
  poll their peers instead of asking for an idle notice. No artifact: approving one records
  it as done by hand, and `stage` refuses it.
- **Coverage says what the recorder missed:** workflow runs that declared more agents than
  were logged, and messages recorded at only one end.
- **model-eval-gate sees sub-agent work by task.** Each invocation becomes one work row
  named by its task, so a sub-agent job repeated across runs is a delegation class
  (`researcher [fanout/verify]`).
- **Fix: the transcript recorder lost an event when a sub-agent transcript grew** between
  reads. It counted the agent's summary record as an event when slicing what was new.

**model-eval-gate is RunTune's route engine.** Four connections, each with a contract test
on both sides:

- **A bake-off writes the eval a route change carries.** model-eval-gate's
  `eval/bakeoff.ts` scores a candidate against the incumbent on the same tasks and writes a
  `runtune.route-eval/1` result; its verdict rules are in `eval/route_eval.ts`. A golden
  result in that repo is checked with RunTune's own validator, so a renamed field fails a
  test rather than an `apply --eval`.
- **One routes.json schema.** model-eval-gate's TypeScript and Python validators reach the
  same verdict on a shared case file, and RunTune validates with the Python one when it is
  installed (otherwise it checks the fields it reads). A file that fails is a finding that
  needs a person. The schema now has `status: "trial"` (a trial mode may omit
  `evidence_ref` and `do_not_use_when`), requires `evidence_ref` on cleared modes, and treats
  `_`-prefixed keys as annotations.
- **Regression drift is a finding.** `--regression` (or a `regression-results.json` next to
  `--routes`) is read in both the current and the older format. Model drift on the routed
  model is a route finding; a run that never reached a provider, or that broke on parsing,
  is withheld with its reason. A clean run after a stale clearance is named on the stale
  finding as the re-check that re-clears it (`eval/regression.ts --route-evals <dir>` writes
  the result to apply). Review marks an applied route change `revalidate` when its mode
  drifts afterwards.
- **Delegation: the agents' own work, through model-eval-gate.** A new deriver runs
  model-eval-gate's workload classes over RunTune's evidence. API-billed repeated tasks it
  would clear except for their stakes become findings that ask that question;
  subscription-billed tasks are one line saying moving them saves $0. Events carry
  `billing` (router calls are `api`; the agents' own turns follow `$RUNTUNE_HOST_BILLING`,
  default `subscription`). model-eval-gate reads RunTune's evidence
  (`meg observe ingest --from-runtune`) and keeps the same redacted text.

Four fixes from the first run over a team's telemetry. Each is
tested over its class in `tests/test_pilot_defects.py`.

- **A missing table or column is a coverage note, for every source.** An older cc-logger has
  no `agent_invocations.token_source`, and RunTune stopped with a traceback. Every warehouse
  source now turns a missing table or column into a coverage note and a warning, and the other
  sources still load. Claude keeps its tool-call evidence when `token_source` (or the whole
  invocation table) is missing and says that sub-agent token figures are not used. A skipped
  OpenRouter source still applies `--routes`.
- **A shell failure is blamed on the part that failed.** Failures were keyed on a command's
  first part, so `report.py … | head; echo ===; …` filed the report's normal output as its
  error and proposed a rule against a command that had worked. The key is now the part whose
  exit status the call reported (the last of a `;` list, the end of a pipeline). An `&&` chain,
  a loop or a truncated recording cannot be attributed; those failures are reported as
  withheld, not counted against any command.
- **One cause breaking several commands is an `environment` finding, not a rule per command.**
  A broken toolchain (`xcrun: unable to load libxcrun`) had become four separate rules, none of
  which could fix it. An error shared by three or more different commands is now one finding
  with no artifact; approving it records the fix as done by hand, and `stage` refuses it.
  Tracebacks, usage lines, warnings, harness timeouts and normal output are not causes.
- **Routes are judged against the policy in force when each call ran.** Calls made while a
  since-retired mode was in force read as unapproved; they are now history. The `routes`
  table used its own status logic and called pre-clearance and benchmark traffic drift; it now
  shares one classifier with `derive`, so the two cannot disagree.

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
