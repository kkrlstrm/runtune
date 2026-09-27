# RunTune

**Your agents already generate the data needed to improve their runtime. RunTune closes the loop.**

[![test](https://github.com/kkrlstrm/runtune/actions/workflows/test.yml/badge.svg)](https://github.com/kkrlstrm/runtune/actions/workflows/test.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue) ![deps](https://img.shields.io/badge/core%20dependencies-0-brightgreen)
![license](https://img.shields.io/badge/license-Apache--2.0-blue)

RunTune is a runtime learning loop for coding agents. It turns real Claude Code, Codex and
model-router runs into governed changes to the system around them.

- **Success → capability.** Working patterns that repeat become skills, CLI paths, specialized
  sub-agents, or better routes.
- **Failure → constraint.** Failures that repeat become guard rules and routing checks.
- **Change → measurement.** Every adopted change is measured against a control, then kept,
  reviewed, or retired.

```
RUN → OBSERVE → DERIVE → APPROVE → APPLY → MEASURE
 ↑                                             │
 └─────────────────────────────────────────────┘
```

**It tunes the harness, not the model.** No weight training, and no autonomous
self-modification. RunTune proposes evidence-backed changes, and a human decides what enters
the runtime.

## The loop

```
                     agent runs
                         ↓
                 execution evidence
                         ↓
      ┌──────────────────┼──────────────────┐
      │                  │                  │
   success            failure             drift
      │                  │                  │
      ▼                  ▼                  ▼
  capability         constraint           route
      └──────────────────┬──────────────────┘
                         ↓
                      propose
                         ↓
                   human approval
                         ↓
                       apply
                         ↓
                      measure
                         │
                         └────────────────↺
```

## It tunes the harness, not the model

A coding agent is a model inside a harness: the skills it can load, the sub-agents it can spawn,
the guards on its tools, and the routes its model calls take. The model is someone else's to
train; the harness is yours to tune. RunTune tunes the harness from what actually ran.

```
               MODEL
                 │
         ┌───────┴────────┐
         │    HARNESS     │
         │  skills        │
         │  sub-agents    │   ◄── RunTune proposes changes here
         │  guards        │
         │  routes        │
         └────────────────┘
                 ▲
                 │
            actual runs
```

**RunTune doesn't teach an agent to remember yesterday. It changes tomorrow's runtime based on
what happened yesterday.** That is the difference from agent memory: the next agent runs in a
different environment, whether or not it recalls anything.

## Observability should close the loop

Most agent observability ends at a dashboard:

```
runs → traces → dashboard → a person reads the dashboard
```

RunTune carries the traces forward into a change, and then checks the change:

```
runs → evidence → candidate change → admission gates → human approval
     → artifact → future runs → measurement → keep · review · retire
```

Traces tell you what happened. RunTune asks what the runtime should learn from it, and whether
the lesson worked.

## Try it in 60 seconds

```bash
pip install git+https://github.com/kkrlstrm/runtune    # zero dependencies
runtune demo                    # the whole loop on a synthetic trace, in a temp dir

runtune record claude           # backfill your Claude Code history (~/.claude/projects)
runtune record codex            # and/or Codex (~/.codex/sessions)
runtune notify                  # one digest: a card + numbered proposals, with a desktop notification
runtune show --open             # read it
runtune reply 1,3               # approve items 1 and 3; the rest are snoozed for 28 days
```

This uses only the transcripts already on your machine: no accounts, no database, no network.
To keep it running, `runtune install` wires the recording hook into Claude Code and Codex, and
`runtune schedule --install` sends a weekly digest. Slack, email and a VM are optional
([channels](docs/CHANNELS.md)).

## What it can change

| from | artifact | what it writes |
|---|---|---|
| repeated failure | **constraint** | a guard rule, capped at what its failure rate supports (nudge, deny, or block) |
| repeated success | **capability** | a `SKILL.md` naming the commands that have worked, for code agents keep re-deriving |
| isolated, repeated work | **sub-agent** | a purpose-built agent type, granted exactly the tools its predecessors used |
| approved vs. ran vs. billed | **route** | a routing check or a retired clearance; a cheaper model always requires an eval first |

Each proposal is compiled to the narrowest artifact that works: a rule if a rule will do, a
sub-agent only if a skill won't. Each proposal also records why the narrower options were
rejected.

## Why it can't run away

- **It proposes; it never applies on its own.** Every change needs a named approver, and there
  is no code path from analysis to a target file.
- **It can't widen its own boundaries.** Loosening a rule, broadening a tool grant, or admitting
  a model needs a written reason, and for models a passing eval. None of these can be approved
  with a one-word reply.
- **Its evidence sets a ceiling.** A failure that happens 30% of the time can earn a nudge, never
  a block.
- **It only edits what it wrote.** It never overwrites a file a person wrote, and it refuses a
  proposal whose target changed after the proposal was reviewed.
- **It stays out of host settings.** It never writes hook wiring or `.git/`, and every decision
  goes into a hash-chained ledger.
- **Ambiguous answers get a question back.** "All but number three" is never read as "all".

## Evidence

RunTune was developed against about 238,000 recorded tool calls and model requests from one
team's production agents: 150 days of Claude Code, Codex and OpenRouter traffic, plus 17,410
sub-agent runs. It found:

- repeated work that should be a reusable capability, including an existing CLI that served
  only 28% of the need it was built for;
- generic sub-agents doing work better suited to narrow, purpose-built ones;
- model traffic bypassing the approved router, which was traced to a helper and fixed;
- guard rules that helped, rules that stopped helping, and one that made failures worse until
  it was rewritten.

**RunTune was wrong too.** Five of its recommendations changed once they were checked against
production. Two apparent routing violations were a clearance-day transition and a benchmark.
One "harmful" rule had already been fixed by its rewrite. A generated rule could never have
fired, and a generated skill named a command path that didn't exist. Each mistake became a regression test, and the evidence doc keeps the wrong versions. A
live three-arm experiment on one of its proposals came back inconclusive, and that result is
published too.

[Read the evidence and methodology →](docs/EVIDENCE.md)

## How it works

- **Record.** RunTune's own hook, or a backfill of Claude Code and Codex transcripts, plus the
  model router's call log and the provider's bill.
- **Derive.** Cluster attempts, grade each cluster against its denominator, and propose.
- **Gate.** Check breadth, signal, replay collateral and observability. Everything withheld is
  reported with its reason.
- **Apply.** The promoter carries out only what a person approved, within the authority rules.
- **Review.** Measure against a control, exclude recorder gaps per source, and flag anything
  that predates a model change.

Details: [how it works](docs/HOW-IT-WORKS.md) · [channels and deployment](docs/CHANNELS.md) ·
[warehouse sources](docs/WAREHOUSE.md) · [extending it](docs/EXTENDING.md).

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). `python3 -m pytest tests -q` runs the governance
invariants, derivers, measurement, hook, recorders and reply parsing.

## License

Apache-2.0. Copyright (C) 2026 Kai Karlstrom. See [NOTICE](NOTICE) for third-party credits.
