# How RunTune works

The README gives the argument; this page gives the mechanics. In order: where the evidence comes from, what it can become, the gates it must pass, the rules the promoter enforces, and how adopted changes are measured.

## 1. Recording: it runs on its own

RunTune records, enforces, learns, and measures without any other tool installed:

| stage | what does it | where it writes |
|---|---|---|
| record Claude Code | `python3 -m runtune.hook --host claude` on PostToolUse / PostToolUseFailure; `runtune record claude` backfills from transcripts, including per-agent tokens | `~/.runtune/events/<day>.jsonl` |
| record Codex | the same hook on Codex, plus `runtune record codex` to read `~/.codex/sessions` rollouts incrementally | same |
| record Cursor | `runtune record cursor` reads Cursor's own store (`state.vscdb`) incrementally, with every outcome; `runtune schedule` runs it before each digest. The hook does not record Cursor, because Cursor's import of Claude Code hooks never delivers failures | same |
| record OpenRouter | `runtune.record.openrouter.log_call()` from your router; `runtune record openrouter` daily for the bill | `events/`, `spend/` |
| enforce | the same hook on PreToolUse (Cursor: `preToolUse`, answered in Cursor's format): monitor / nudge / deny / block, most restrictive wins, capped at each rule's evidence ceiling, fails open | `~/.runtune/audit.jsonl` (hash-chained) |
| learn, notify, apply, measure | `derive`, `notify`, `inbox`, `review` | `.runtune/` |

All recorded text is redacted before it touches disk. If you already run a telemetry warehouse
(cc-logger, codex-logger, cursor-logger, a router call log), point `--db` at it: the same derivers read it
directly. The standalone Codex recorder and codex-logger agree to within 4% on the same rollout
files (2,412 against about 2,500 settled calls).

## 2. What evidence can become: four artifact kinds, one ladder

Every cluster of evidence is compiled to the **narrowest artifact that works**. The order is
taken from AutoRefine, and each candidate records why the narrower rungs don't work:

| rung | artifact | derived from | written to |
|---|---|---|---|
| 1 | **constraint** | failure clusters, graded against every attempt of the same shape | a CallusGuard-compatible ruleset |
| 2 | **capability** | programs agents keep re-writing to reach the same code; recurring successful procedures | `.claude/skills/<name>/SKILL.md` |
| 3 | **subagent** | fan-outs of generic agents whose measured toolset is narrow | `.claude/agents/<name>.md` |
| — | **route** | the router's call log, the provider's bill, and the allowlist, compared with each other | `routes.json` / `route-checks.json` |

## 3. Admission: gates that run before a human reads anything

- **Denominator.** Failure tiers come from CallusGuard. A rate over fewer than 5 attempts is
  `anecdotal` and is never proposed. The tier caps the action: `probabilistic` may nudge,
  `reproducible` may deny, and only `deterministic` may block. `apply` enforces the cap.
- **Breadth.** One session retrying 40 times is one incident. Constraints need 2 or more
  sessions. Capabilities need 5 or more sessions across 2 or more weeks.
- **Replay** (AutoRefine's replay gate, done without executing anything). Each candidate
  pattern is run over every recorded attempt. Failures it matches are its *correction* cases;
  successes it matches are its *preservation* cases, meaning collateral. A pattern whose
  matches are mostly successes is flagged for a human to narrow before it can be armed.
- **Signal.** Builtins, inline interpreters, and read-only exploration (`cd`, `python3 -c`,
  `ls`, `cat`) are rejected with a stated reason. They form real clusters, but a rule on them
  would fire on almost every command.
- **Observability.** An agent is never narrowed off a tool the recorder cannot see. A warehouse
  recorder that captures only an allowlist of tools can't show `Grep` as used, so "never used"
  would be false.
- **Cross-source.** A route verdict from the calls log is checked against the provider's bill
  before it is proposed.

Withheld candidates are always reported with the gate that stopped them, so a report of
"nothing to propose" can't hide "103 that failed a gate".

## 4. Authority: the learner can't rewrite its own boundaries

`apply` is the only code that writes outside `.runtune/`, and it refuses in these cases:

1. **No approver.** Every apply records a named person. No code path runs from `derive` to a target file.
2. **Widening without a reason.** This covers retiring a constraint, broadening a tool grant, or
   admitting a model to a mode. It needs `--reason` and is logged as a boundary change. Routes
   also need `--eval` pointing at a passing eval result.
3. **Past the tier's ceiling.** `block` on a probabilistic failure is refused.
4. **Someone else's file.** RunTune overwrites only files it created and marked. A revision
   declares its lineage with `--revises` and inherits every predecessor's cases, so a fix can't
   silently regress an earlier fix. Grant revisions may only remove tools.
5. **A stale target.** A proposal reviewed against one version of a file is refused against
   the next one.
6. **Protected paths.** Host settings, hook wiring and `.git/` are never written.

Every stage, apply and retire goes into a hash-chained ledger (`runtune ledger`).

## 5. Measurement: three errors it guards against

Each of these produced a wrong answer in this project's own history before it was fixed:

- **No control.** Every before/after reports the target's change next to the change in
  comparable traffic the artifact could not have touched, and gives the difference.
- **Reading only rates.** A nudge that makes agents stop reaching for a command leaves the
  hard cases behind, so the failure rate rises while behavior improves. Attempt share is
  printed next to every rate. (`bare-psql`: 7.6% → 22.6% while attempt share fell 90%.)
- **Recorder gaps.** Days a recorder was down are excluded **per source**. An earlier version
  pooled them, and a 61-day Codex outage erased two months of Claude data from every
  before/after. The regression test for that bug is in the suite.

A change of model is also a reason to re-check. `scan` finds the weeks where the dominant
model changed, and `review` marks any artifact whose evidence predates that week as
`revalidate`. The guard A/B this came from found 3 of 13 rules could no longer be triggered
after a model upgrade.
