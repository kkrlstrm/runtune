# Lineage: what came from where, and what was changed

RunTune merges three projects that each covered part of one loop. This file records what was
taken from each, how it was changed, and what was left out and why.

## CallusGuard (failure → constraint). Same author, Apache-2.0.

| taken | where it lives now |
|---|---|
| Failure tiers (deterministic / reproducible / probabilistic / anecdotal / unknown) and the action ceiling each tier allows | `runtune/evidence/tiers.py` (ported unchanged) |
| Command shape and error signature as the denominator and numerator keys | `runtune/evidence/shapes.py`, extended with URL collapse, prefix stripping, and inline-program targets |
| Withheld clusters are reported, never dropped silently | every deriver returns `(proposed, withheld)` |
| Probe vs prune: a quiet rule is a question, not a success | `lifecycle/review.py` → `probe` |
| Hash-chained audit log | `lifecycle/ledger.py` |
| cc-logger and codex-logger schemas | `sources/claude.py`, `sources/codex.py` read them directly |

**Changed:**

- Clusters are keyed per command shape, not per error signature. Many signatures compiled to
  the same regex, which produced duplicate candidates.
- Claude `Bash` and Codex `exec` are treated as one shell surface.
- A replay gate measures collateral before a human reads the candidate.
- Enforcement stays in CallusGuard. RunTune writes rulesets in its format.

## autoharness (success → skill, Claude Code). tigerless-labs, MIT.

| taken | where |
|---|---|
| The model proposes, deterministic code writes; one set of checks at authoring and at apply | `lifecycle/promoter.py` is the only writer outside `.runtune/` |
| Evidence stored content-addressed and redacted, never named by a model | `Workspace.put_evidence` |
| Touch only files you wrote (a `created_by` sidecar) | `.runtune.json` sidecar and the `runtune-artifact` marker; `_owned()` |
| Use rate against opportunity, not raw use count | `measure.adoption`: the new way's share of (old + new) |
| Merge vs death in the ledger | `--revises` lineage; `retire` records a reason |
| A skill description must say *when* to use it | generated SKILL.md descriptions begin "Use when" |

**Changed or rejected:**

- autoharness mines every 50-tool-call window "liberally" and relies on archiving to clean up.
  RunTune derives only from clusters that pass the breadth gate, because each junk skill costs
  a reviewer's time before archiving ever runs.
- autoharness's evidence quotes are never checked against the transcript. RunTune's evidence
  is built from recorded rows, not from model-written quotes.
- It has no human approval step. RunTune requires a named approver for every apply.
- It covers Claude Code only. RunTune reads three sources.

## AutoRefine (typed artifacts: rule / skill / subagent). arXiv 2601.22758; the repo has no LICENSE file.

Ideas were reimplemented; no code was copied.

| taken | where |
|---|---|
| Compile order rule → skill → subagent, with a written reason why each narrower rung does not work | `Candidate.ladder` on every candidate |
| Correction evidence and preservation evidence | `Candidate.correction` / `.preservation`, filled by replay |
| The replay gate: no regression on preservation cases | done offline over recorded attempts (`constraints.replay`); nothing is executed |
| Revisions inherit all predecessor cases | `promoter.stage(..., revises=)` concatenates `cases` |
| Lineage is declared, never inferred from names | `--revises` is required; a name collision is refused |
| A disabled gate still runs and records its verdict | `Gate.suppressed` |
| A subagent must return a status that separates success from failure | subagent contract lint; generated agents declare `status` |
| Stale-parent refusal | `base_digest` checked at apply |

**Changed:**

- AutoRefine's replay re-runs tasks in a benchmark environment. GTM work cannot be re-run, so
  RunTune replays patterns over recorded history instead. That establishes collateral; it does
  not establish that the artifact would have fixed the failure.
- AutoRefine admits artifacts automatically once its gates pass. RunTune never does.
- AutoRefine v2 has no pruning (v1's score-and-prune was dropped). RunTune's `review` retires.

## Knowledge-hub prior art (read, not vendored)

| idea | source | where |
|---|---|---|
| Tiered write authority; the safety tier is read-only to the learner | Anthropic "context engineering at scale" memory guardrails talk | `authority.json` protected paths; widening needs a reason |
| Any widening of a capability ceiling invalidates the change | Harn (mechanism 6), Capa `--fail-on-widening` | grant revisions may only remove tools |
| Artifacts stamped with the policy epoch they were validated under | AgentJail, Cedar | `policy_digest` and model epochs → `revalidate` |
| One-off stumbles vs distribution-level issues, kept apart | Raindrop | the breadth gate |
| A failure is one sample from an environment; reject fixes that regress earlier cases | RELAI | inherited cases on revision |
