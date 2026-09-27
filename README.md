# RunTune

**Agents should get better from their runs.**

RunTune turns real agent execution into better future behavior. Successful patterns become
reusable capabilities. Recurring failures become reviewed constraints. Both are measured,
refined, and retired when they stop helping.

Runs are the evidence; tuning is what happens afterward. This is not model training, and it
is not autonomous self-modification: the learner proposes, a named person applies, and the
learner cannot widen its own boundaries.

```
                    REAL AGENT WORK   (Claude Code · Codex · OpenRouter)
                          │
                   recorders (cc-logger, codex-logger, router call log, provider bill)
                          │
                  execution evidence  — one stream, settled attempts only, gaps named
                          │
              ┌───────────┴───────────┐
              │                       │
       SUCCESS / REUSE             FAILURE / DRIFT
              │                       │
       derive capability          derive constraint
     (skill · CLI · subagent ·   (guard rule · route check ·
        cheaper route)             retired clearance)
              │                       │
       "do this again"          "don't do this again"
              │                       │
              └───────────┬───────────┘
                          │
            stage → human applies → future agents
                          │
          measure outcomes (with a control, per model epoch)
                          │
          keep · review · probe · retire · revalidate
                          │
                          └──────↺
```

## What it does, on one machine's real data

Run against 150 days of one operator's telemetry: 165,224 Claude Code attempts, 2,515 Codex
attempts, 70,364 OpenRouter requests plus the provider's own bill, and 17,410 agent
invocations. That run's proposals were then acted on, and checking each one against reality corrected
RunTune five times ([Part 3 of the evidence](docs/EVIDENCE.md#part-3-adopting-its-suggestions-and-what-that-showed-2026-09-27)). A sample of what came out:

| side | finding | why it matters |
|---|---|---|
| capability | 5,899 inline Python programs across 406 sessions existed only to import one internal library. A CLI for it already existed, and in the two weeks after launch it served **28%** of that need. | Adoption gap: the capability exists and most agents aren't using it. The derived skill names only CLI invocations that have succeeded in the recorded runs. |
| subagent | 5,669 generic workflow sub-agents across 27 fan-outs used only `Bash, Read, WebFetch, WebSearch`. They re-read **263** tokens of context per output token; the existing purpose-built types re-read **115**. | Proposes a narrow type whose grant is the measured census. It reports **no** saving for the `general-purpose` fan-outs, which already re-read only 44:1. |
| route | Two modes looked idle to the router, but the bill showed their models running **630** and **1,095** requests outside it. | Traced to a production helper calling OpenRouter over direct HTTP with no ledger record. **Fixed and verified live.** RunTune proposes routing the caller through the chokepoint, not retiring a clearance that is in use. |
| route | A first version reported two "drift" findings. | **Both were false positives:** one was traffic on a clearance day before the swap took effect, the other a benchmark's failures. Both are now handled with tests. The evidence doc keeps the wrong version. |
| route | **10,046** billed requests on 16 models never passed through the router; 14 of those models are on no allowlist. | This is the traffic an allowlist cannot see. |
| constraint | 14 live guard rules, every version measured at its own date against a control. One cut failures **19.1 points** beyond the control. One **raised** them 26.4 points because its message named a flag that didn't exist; **its rewrite cut them 8.8 points**. | Rules have a life. Measuring only a rule's first version condemned one its rewrite had fixed. |
| experiment | The derived `ttt` skill and nudge in a live three-arm test (36 headless runs). CLI share 2/12 without the skill, 8/23 with it; p = 0.43, and the nudge caused 0 switches within a run. | The artifact was adopted as tracked, and `review` measures its real adoption. An effect that one small test can't establish is measured over time, not assumed. |

The full tables, the method, and every limit are in [docs/EVIDENCE.md](docs/EVIDENCE.md).

## The loop

```bash
pip install -e .                    # zero dependencies; '.[postgres]' adds warehouse sources
runtune install                     # prints the hook wiring for Claude Code and Codex

runtune scan                        # what the evidence covers, and where it has holes
runtune derive                      # proposals, each with its evidence and gates
runtune notify --channel slack      # ONE digest: a card image + numbered proposals
runtune inbox                       # reads your reply ("1,3", "all except 2", "none") and applies
runtune review                      # keep · review · probe · retire · revalidate
```

The same actions are available directly: `runtune stage <id>`, `runtune apply <id> --approve
kai`, and `runtune retire <id> --approve kai --reason …`.

## It runs on its own

RunTune records, enforces, learns, and measures without any other tool installed:

| stage | what does it | where it writes |
|---|---|---|
| record Claude Code | `python3 -m runtune.hook --host claude` on PostToolUse / PostToolUseFailure | `~/.runtune/events/<day>.jsonl` |
| record Codex | the same hook on Codex, plus `runtune record codex` to read `~/.codex/sessions` rollouts incrementally | same |
| record OpenRouter | `runtune.record.openrouter.log_call()` from your router; `runtune record openrouter` daily for the bill | `events/`, `spend/` |
| enforce | the same hook on PreToolUse: monitor / nudge / deny / block, most restrictive wins, capped at each rule's evidence ceiling, fails open | `~/.runtune/audit.jsonl` (hash-chained) |
| learn, notify, apply, measure | `derive`, `notify`, `inbox`, `review` | `.runtune/` |

All recorded text is redacted before it touches disk. If you already run a telemetry warehouse
(cc-logger, codex-logger, a router call log), point `--db` at it: the same derivers read it
directly. The standalone Codex recorder and codex-logger agree to within 4% on the same rollout
files (2,412 against about 2,500 settled calls).

## How suggestions reach you

Nothing is applied automatically, and nothing waits in a file for you to find.

1. **Weekly, `runtune notify`** derives and reviews, then sends one digest. It is a card image
   in the style of a weekly client report: a status chip, three tiles, and the adopted
   artifacts' measured effect as bars. A numbered list of up to 8 proposals comes with it. It goes
   to a **Slack DM**, to **email** (Gmail API thread or SMTP), or to stdout.
2. **You reply with numbers.** `runtune inbox`, run hourly and free when idle, reads replies
   from the approver only and acts on them:
   - `1,3` · `all` · `all except 2` · `none` are carried out.
   - Anything else ("all but number three", "the first two") gets a clarifying question in the
     thread, never a guess.
   - Declined items are snoozed for 28 days.
3. **Some items can't be approved by reply.** A widening (a looser rule, a broader grant, a model
   admitted to a mode) must be applied from a terminal with `--reason`, and `--eval` for routes.
   A constraint whose own replay shows it would mostly fire on working calls goes on a "needs a
   person to narrow it" line. A proposal already covered by a rule in force is not offered at
   all.
4. **`review` feeds the next digest.** An adopted artifact that made things worse, went quiet, or
   predates a model change comes back flagged.

| channel | env |
|---|---|
| Slack DM | `RUNTUNE_SLACK_TOKEN` (bot: chat:write, im:write, im:history, files:write), `RUNTUNE_SLACK_USER` |
| email | `RUNTUNE_GMAIL_TOKEN` (an authorized-user token file) or `RUNTUNE_SMTP_*`; `RUNTUNE_EMAIL_TO` |
| approver | `RUNTUNE_APPROVER`: the name written on every apply |

`RUNTUNE_DRY_RUN=1` refuses every send.

## As an agent on a VM (optional)

`deploy/fly/` runs the loop unattended on one small Fly machine:

- **Weekly:** derive + review + digest.
- **Hourly:** inbox.
- **Daily:** an OpenRouter bill snapshot.

On a VM the targets live in a git repo, so `inbox --git` applies approvals into a clone,
commits them on a branch and opens a pull request. Merging is the final approval, and
`git revert` is the undo.

Secrets are named one by one (`deploy/fly/set-secrets.py`), never a whole `.env`. Give it a
SELECT-only database role: RunTune opens read-only sessions, but a read-only session is a
setting, not a permission.

## Four artifact kinds, one ladder

Every cluster of evidence is compiled to the **narrowest artifact that works**. The order is
taken from AutoRefine, and each candidate records why the narrower rungs don't work:

| rung | artifact | derived from | written to |
|---|---|---|---|
| 1 | **constraint** | failure clusters, graded against every attempt of the same shape | a CallusGuard-compatible ruleset |
| 2 | **capability** | programs agents keep re-writing to reach the same code; recurring successful procedures | `.claude/skills/<name>/SKILL.md` |
| 3 | **subagent** | fan-outs of generic agents whose measured toolset is narrow | `.claude/agents/<name>.md` |
| — | **route** | the router's call log, the provider's bill, and the allowlist, compared with each other | `routes.json` / `route-checks.json` |

## Admission: gates that run before a human reads anything

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
- **Observability.** An agent is never narrowed off a tool the recorder cannot see. cc-logger
  does not capture `Grep`/`Glob`, so "never used" would be false.
- **Cross-source.** A route verdict from the calls log is checked against the provider's bill
  before it is proposed.

Withheld candidates are always reported with the gate that stopped them, so a report of
"nothing to propose" can't hide "103 that failed a gate".

## Authority: the learner can't rewrite its own boundaries

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

## Measurement: three errors it guards against

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

## What this is not

- **Not a sandbox.** Constraints are advisory to blocking hooks; for isolation use an OS
  sandbox, and see CallusGuard's threat model.
- **Not causal.** Every rate is observational, over traffic that happened to run. The control
  makes before/after defensible, not experimental.
- **Not a quality oracle for models.** OpenRouter `ok` means the request returned. A model
  can be 100% ok and worst of five on quality. Route capabilities are always `eval required`.
- **Not a skill writer.** The derivers are deterministic counters with templates: they find
  what recurs and attach the evidence. The generalizing step is human, or a drafting model
  whose output goes through the same promoter.

## Lineage

RunTune merges three projects that each covered part of this loop. See
[docs/LINEAGE.md](docs/LINEAGE.md) for what came from each, and why some ideas were changed
or left out.

- **[CallusGuard](https://github.com/kkrlstrm/callusguard):** the failure side. Contributes
  failure tiers with action ceilings, the rule lifecycle, the probe-vs-prune distinction, and
  the hash-chained audit log, all ported. RunTune has its own recorder and enforcement hook and
  does not depend on CallusGuard. Its rulesets use the same shape, so either tool can enforce
  them.
- **[autoharness](https://github.com/tigerless-labs/autoharness) (MIT):** the success side for
  Claude Code. Contributes the split where a model proposes and deterministic code writes,
  content-addressed evidence, use-rate against opportunity, merge versus death in the ledger,
  and the rule that RunTune only ever touches files it wrote.
- **[AutoRefine](https://github.com/AutoRefine/Autorefine)** ([arXiv 2601.22758](https://arxiv.org/abs/2601.22758)):
  typed artifacts. Contributes the rule → skill → subagent compile order, correction plus
  preservation evidence, the replay gate, revisions that inherit their predecessors' cases,
  declared lineage, and suppressed gates that still record their verdict. These are
  reimplemented; no code was copied, because that repo ships without a license file.

## Tests

```bash
python3 -m pytest tests -q      # governance invariants, derivers, measurement, hook, recorders, reply parsing
```

## License

Apache-2.0. Copyright (C) 2026 Kai Karlstrom. See [NOTICE](NOTICE) for third-party credits.
