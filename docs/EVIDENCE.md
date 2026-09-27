# Evidence

Two kinds of evidence support RunTune. The first is prior experiments from the system it was
built from (the GTM Bench series), which motivated its design choices. The second is RunTune
itself run against that system's real telemetry. Every number below was produced by a
command in this repo or by the source named next to it. Client names, contact data and
message bodies are left out on purpose; everything shown is an aggregate.

## Part 1: prior results that set the design

| finding (source) | what RunTune does because of it |
|---|---|
| **A written document helps a model that cannot do a task and hurts one that can.** Six document variants × seven models × two real tasks, about 22,000 scored answers. On the task models could not do, documents added about 9 points. On the task they could do, the same documents cost up to 14.5 points. A placebo document moved nothing, twice. *(GTM Bench ep. 1, scaffold-transfer findings)* | A capability is never assumed to help. Every skill is reviewed on outcome after adoption, and a model change marks it `revalidate`. A skill measured under one model is a claim about that model. |
| **A model upgrade made 3 of 13 guard rules impossible to trigger**, including one that had fired 199 times. A fixed-model A/B showed guards moved tokens by +0.2%, below the controls' own +1.1% and +2.2%. *(ep. 2, guard-ab benchmark)* | Model epochs are detected per source (`runtune scan`). Artifacts whose evidence predates an epoch go to `revalidate`, and a quiet rule is a `probe` question, not a keep. Every before/after has a control. |
| **Model tier bought almost nothing on these tasks:** seven models across a 30x size range landed in the same narrow band. *(ep. 3)* | Route capabilities (a cheaper model for a mode) are proposed from real traffic, but always marked `eval required`. |
| **A model on no allowlist ran 1,464 production requests** and classified 34,162 rows; scored later, it was worst of five. *(ep. 4)* | The route deriver compares the allowlist, the router's call log, and the provider's bill. Drift, unapproved traffic and unrouted spend are constraint candidates. |
| **The grader was wrong four times before the models were:** accuracy tracked position bias (r = +0.88), a grader scored every arm 0.0%, a cost estimate was 50x low, and a container ran stale code. *(ep. 5)* | The instrument is checked first. `scan` states coverage and gaps before any derivation. Token figures are used only from per-agent-verified rows (below). |
| **Agent throughput peaks at 5–8 concurrent agents.** The sub-agent token columns had been inflated 38.9x by a root-transcript broadcast bug, and the check agreed because session totals were sums of the same rows. *(ep. 6)* | Invocation tokens are read only where `token_source = 'transcript-verified'`. The subagent deriver says "projection, not a measurement" whenever it cites token ratios. |
| **Generic workflow sub-agents re-read ~177 tokens per output token**, against 23–59 for narrow purpose-built types. *(8,824 invocations; find-dials preamble study)* | The subagent deriver proposes narrow types whose tool grant is the measured census, and says when no saving is expected. |
| **CallusGuard's funnel:** 136 derived candidates, 12 promoted (9%). 97 days of enforcement; two promoted rules demonstrably did nothing or made things worse. *(callusguard README)* | The same funnel shape, now across four artifact kinds. Withheld candidates are reported with their failing gate. `review` flags a rule that raised failures. |

## Part 2: RunTune on real telemetry (2026-09-27)

### What the evidence covers (`runtune scan --days 150`)

| source | settled attempts | window | unsettled (excluded) | recorder gaps |
|---|---:|---|---:|---|
| Claude Code (cc-logger) | 165,224 | 05-13 → 09-27 | 6,318 | 07-08→07-13 (6d), 09-09→09-13 (5d) |
| Codex (codex-logger) | 2,515 | 05-05 → 09-10 | 348 | 7 gaps incl. 05-06→07-05 (61d) |
| OpenRouter calls | 70,364 | 09-08 → 09-25 | — | — |
| OpenRouter bill | per-day, per-model activity | 07-31 → 09-25 | — | — |
| agent invocations | 17,410 | | | |

Model epochs detected: Claude `opus-4-7 → 4-8` (W23) and `4-8 → opus-5` (W32); Codex
`gpt-5.4-mini → 5.5 → 5.6-luna → 5.6-terra` (W30–W33).

Codex coverage is thin and has large gaps, and every Codex-derived number should be read with
that in mind. The recorder needs a completeness check more than any further derivation.

### The funnel (`runtune derive --days 150`)

| kind | proposed | withheld |
|---|---:|---:|
| constraint | 29 | 40 |
| capability | 7 | 48 |
| subagent | 8 | 12 |
| route | 7 | 3 |
| **total** | **51** | **103** |

Why withheld: breadth 67, signal 25, replay-collateral 10, narrower-than-host 7,
current-policy 3, denominator 2. All 29 proposed constraints are `probabilistic` or
`reproducible`; none earned `block`.

### Existing guard rules, measured against a control

`runtune replay --ruleset live.rules.json --measure --days 150` measures each rule at its promotion date (`meta.added`), over 28-day windows on both sides. The
control is all other shell traffic over the same days, and the net figure is the rule's
change minus the control's change.

| rule | before | after | net vs control | attempt share | reading |
|---|---|---|---:|---:|---|
| page-digest-missing-entity | 19.8% (126) | 1.7% (115) | **−19.1** | −9% | works |
| shell-source-dotenv | 16.1% (248) | 4.8% (271) | **−15.4** | +8% | works |
| bash-busywait-poll-loop | 15.6% (90) | 2.5% (118) | −9.4 | +65% | works |
| macos-timeout-not-installed | 27.0% (37) | 17.9% (134) | −5.5 | +358% | probably works; attempts grew fast |
| bash-cd-relative-cwd-drift | 13.3% (120) | 10.0% (10) | −4.4 | −85% | agents stopped; thin after |
| bash-inline-shell-function-def | 6.4% (47) | 3.3% (30) | −3.2 | −74% | agents stopped |
| bash-sleep-chained-command | 4.5% (44) | 0.0% (18) | −3.1 | −54% | agents stopped |
| phoneburner-db-hand-rolled | 19.0% (79) | 21.1% (19) | +3.5 | −73% | agents stopped; residual hard cases |
| curl-page-scrape-spoofed-ua | 0.0% (97) | 5.6% (18) | +9.2 | −77% | agents stopped; residual |
| bare-psql-no-target | 7.6% (952) | 22.6% (93) | +10.7 | **−90%** | behavior changed; the rate rose on the residual cases |
| page-digest-dead-domain-retry (v1, 07-06) | 7.7% (704) | 34.4% (607) | **+26.4** | −6% | **made failures worse**; the 07-27 rewrite cut them −8.8 (Part 3) |

Four rules had fewer than 5 attempts on one side and are reported as insufficient.

The "before" figures reproduce CallusGuard's published ones independently: `bare-psql` 7.6%
over 952 attempts (published: 7.6% over 980), and `shell-source-dotenv` 16.1% over 248
(published: 16.1% over 249). Two verdicts differ from CallusGuard's all-time, uncontrolled
table. `macos-timeout` was reported as having done nothing; `curl-page-scrape` was reported as
improving (2.0% → 0.0%). Part of each difference is the window length, not only the control.

### The same rules replayed across hosts (`runtune replay`)

Rules derived only from Claude Code traffic also match Codex traffic. For example,
`shell-source-dotenv` matched 11 Codex commands, `bash-inline-shell-function-def` 16, and
`bare-psql-no-target` 4. A constraint learned on one host is evidence about the other.

### Capabilities

| finding | numbers |
|---|---|
| internal-library adoption gap | 5,899 inline programs, 406 sessions, 17 weeks, mean 574 chars. A CLI exists: 296 CLI calls against 766 inline programs in the two weeks after launch (**28%** share). 23 distinct functions cover 95% of the calls inside those snippets. |
| six other local libraries re-derived inline | 24–80 snippets each across 5–14 sessions; three already have a CLI (adoption gaps) |

### Subagents

| agent type | invocations | completed | re-read / output (verified rows) | census |
|---|---:|---:|---:|---|
| workflow-subagent (generic) | 8,871 | 86% | 263 (1,532 rows) | Bash 84%, WebSearch 12%, WebFetch 5%, Read 2% |
| general-purpose (generic) | 1,323 | 95% | 44 (252 rows) | Bash 65%, WebSearch 48%, Write 43% |
| web-researcher (narrow) | 264 | 95% | 142 (34 rows) | Bash 94%, WebSearch 61%, WebFetch 51% |
| dial-researcher (narrow) | 435 | 99.8% | 255 (27 rows) | Bash 100% |

The narrow types have few verified-token rows, so their ratios are indicative only. For
`general-purpose`, the proposal explicitly says no token saving is expected.

### Routes

> The two **drift** rows below were later shown to be false positives (Part 3). They are kept
> as the first version reported them.

| mode | model | status | calls | ok | $/ok call |
|---|---|---|---:|---:|---:|
| extract-accurate | deepseek-v4-flash | approved | 57,566 | 99.1% | 0.00031 |
| research-dial-fast | gemini-3.1-flash-lite | approved | 5,948 | 99.7% | 0.0051 |
| research-dial-fast | gpt-5.6-luna-pro | **drift** (1,902 after clearance) | 3,440 | 99.1% | 0.0107 |
| research-dial-second | gpt-5.6-luna-pro | approved | 552 | 100% | 0.0077 |
| research-dial-second | gemini-3.1-flash-lite | **drift**, 32% fail after clearance | 540 | 84.8% | 0.0029 |
| extract-bulk / filter-auto-reply | — | idle to the router, **billed 630 / 1,095 requests outside it** | 0 | | |
| lint-code | — | idle and 132 days since verification | 0 | | |

A mistake this caught in RunTune's own development: the first version proposed retiring
`extract-bulk` and `filter-auto-reply` as idle. An end-to-end test applied that retirement to
a scratch copy of the allowlist. Once the bill was compared with the call log, it showed both
modes in active use by callers that bypass the router. Retiring them on the router's evidence
alone would have removed a clearance production depends on. The finding now reads "route the
bypassing caller through the chokepoint", and a test covers it.


## Part 3: adopting its suggestions, and what that showed (2026-09-27)

RunTune's first real digest was acted on the same day. The table records each suggestion,
what was done, and what checking it against reality showed. Five of the findings changed
RunTune itself; each is fixed and has a test.

| suggestion | what was done | what reality showed |
|---|---|---|
| **route drift:** `research-dial-fast` ran 1,902 requests on an off-policy model after its clearance | traced by the hour | **False positive.** The two dial modes swapped models at about 11:00 UTC on their clearance day, and every "drift" call came from that morning. A date-only clearance can't say what hour it took effect. RunTune now treats the clearance day as a transition day. |
| **route unreliable:** flash-lite fails 32% of `research-dial-second` | traced the callers | **False positive.** 81 of the 248 failing calls came from `transport_bench.py`, a benchmark. RunTune now separates benchmark and eval callers from production. |
| **idle-but-billed modes:** two approved models billed outside the router | traced the callers | **Real.** `scripts/lib/ttt/directory.py` called OpenRouter over direct HTTP (navigator and extractor) and never recorded to the ledger. **Fixed:** both calls now record mode, model, cost, tokens and latency. Verified live: 2 real calls produced 2 ledger rows, 70,364 → 70,366. The navigator records under `directory-nav`, which is on no allowlist, so it now appears as unapproved traffic until it is cleared with its existing eval. The rest of the bill gap is benchmark traffic from 09-07 to 09-09. |
| **guard rule harmful:** `page-digest-dead-domain-retry` raised failures 26.4 points beyond the control | measured every version | **Half wrong.** Version 1 (07-06) did raise failures: its message named a flag that didn't exist, and calls passing that flag spiked to 53 in one week. The 07-27 rewrite fixed it, cutting failures 8.8 points beyond the control over 28 days (15.6 over 21), and the bad flag disappeared. RunTune had measured only version 1. `replay --measure` now measures every version (`meta.added` and `meta.updated`). |
| **capability:** a `use-ttt-cli` skill plus a companion nudge, derived from 5,899 inline programs | a live three-arm experiment: headless Sonnet, the repo as-is (A), plus the skill (B), plus the skill and RunTune's own hook enforcing the nudge (C) | See below. **Adopted through RunTune's own promoter**, so `review` will measure real adoption over the coming weeks. |
| **subagent:** generic workflow fan-outs using only `Bash/Read/WebFetch/WebSearch` should get a purpose-built type | already done on 09-11, before RunTune existed | Measured after the fact. Narrow types were 0–25% of sub-agent invocations in the weeks before and **90%, then 81%** in the two weeks after. |
| **lifecycle:** `lint-code` is idle for 150 days and 132 days past verification | **not applied** | It is still named in `or_router.py` and a self-test asserts the two agree, so retiring it is a change to shared policy. It stays in the digest for a person to decide. |

Three more defects surfaced while producing the first real digest:

- **A generated pattern that could never fire.** The shape is keyed on the script's basename,
  but agents type `python3 scripts/page-digest.py`, and the pattern required the two words to
  be adjacent. The replay gate passed it anyway: 0 matches meant 0 collateral. Patterns now
  allow a path prefix, and a new gate refuses a pattern that doesn't match at least half of
  the failures it was derived from.
- **A skill that pointed at a command that doesn't exist.** The first draft wrote `ttt db
  master`, but the command is `scripts/ttt`. Skills now cite commands exactly as typed in
  successful calls.
- **A reply reader with no lower bound.** When Slack had not yet returned the digest's
  timestamp, the reader would have scanned the whole DM history, so an old message could be
  read as an approval. Reads are now floored at the send time.

### The capability experiment

`experiments/ttt-skill-ab/`: four read-only tasks, three arms, headless Claude Sonnet runs,
arms shuffled within each task-and-rep block, and ground truth computed beforehand.

**Round 1 (24 runs) measured nothing.** It is kept to show why. Every answer was correct, but
38 of 50 tool calls read `config/clients.json` directly: the tasks had a cheaper path than
the library, so they could not test a skill about the library.

**Round 2 (36 runs)** used tasks that can only be answered through the database or GitLab.

| arm | runs | answered correctly | reached `ttt` by CLI | by inline program |
|---|---:|---:|---:|---:|
| A: repo as-is | 12 | 12 | 2 | 10 |
| B: + skill | 12 | 12 | 3 | 9 |
| C: + skill + nudge hook | 12 | 12 | 5 | 6 |

- **Skill present vs absent:** CLI share 8/23 against 2/12. The direction favours the skill,
  but Fisher's exact p = 0.43, so the effect is not established.
- **The skill was never explicitly invoked** in 24 chances. Any effect runs through its
  one-line listing, not through loading it.
- **The nudge caused 0 switches** in the 6 runs where it fired. The inline call it nudges
  still runs and returns the answer, so on a one-question task there is no next call to
  change. A nudge can only change behavior across calls, which means across sessions, and
  that is what `review`'s adoption measurement covers.
- **Cost of the three arms:** identical accuracy (36/36), and median wall time 18.4 s, 15.1 s
  and 18.2 s.

**Conclusion.** RunTune found a real, established pattern: 5,899 re-derived programs. The
artifact it generated is safe and cheap, but a controlled test at this size can't show that it
changes behavior. That is not a reason to keep or drop it on belief. It was adopted as a
tracked artifact, and `review` will report its adoption share against the 28% baseline on
real traffic. If adoption doesn't move in four weeks, review proposes retiring it.

## Limits

- **One operator and one codebase.** Nothing here is a claim about anyone else's agents.
- **Observational.** No rate here came from a controlled re-run. Controls make the
  before/after comparisons defensible; they do not make them experiments.
- **28-day windows** are a choice. Longer windows gain sample size and lose attribution.
- **The capture allowlist.** cc-logger does not see every tool, and Codex coverage has gaps of
  up to 61 days.
- **`ok` is not quality.** No route finding says a model is good.
- **The derivers are counters.** They find what recurs; a human decides what it means.
