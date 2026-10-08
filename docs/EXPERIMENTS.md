# Regularized experiments (experimental foundations)

RunTune's existing `verify`, approval ledger, promoter, and deployment boundaries remain authoritative.
These dependency-free Python APIs are **advisory** and are not yet wired into the CLI.

## Hold out evaluation tasks

Separate cases **before** proposal generation into development, regression, and held-out
sets. Group related tasks by originating session or project to prevent near-duplicate leakage.
Do not show held-out prompts, expectations, or results to the proposer. Keep their hashes
and split assignment in a versioned manifest, and never reassign after seeing scores.
Use held-out results only for final candidate assessment; repeatedly tuning to them
invalidates the holdout. Preserve RunTune's existing containment and task-review rules.

## Candidate screening

`runtune.experiment_selection.assess_candidate(control, treatment, ...)` takes
`{"successes": 40, "trials": 100, "total_cost": 10.0}` for each arm.
It returns an **advisory** recommendation, absolute success-rate change, Wilson
intervals, and per-trial relative cost change. Defaults require 20 trials per arm,
a quality gain greater than the declared noise floor, non-overlapping 95% Wilson
intervals, and no cost increase. Missing cost holds the candidate rather than
silently assuming zero. For correlated repeated tasks, use a paired or clustered
analysis in the existing verifier; Wilson screening alone does not establish a
causal effect or correct for multiple comparisons.

Only compare candidates against a **shared frozen baseline** and reserve an
untouched final test set for the selected winner. Running many candidate screens
without correcting for multiple comparisons inflates false positives. Never treat
`eligible_for_review` as an approval or pass result.

## Hypothesis history

`runtune.experiment_registry.append_hypothesis(...)` records a candidate hash,
hypothesis, verdict, and evidence in a chained JSONL history. `read_history`
verifies the chain before reading. The file is tamper-evident, **not authenticated**:
someone who can rewrite the whole file can recompute the chain. For stronger
integrity, anchor the latest digest outside the writable directory. Do not put
secrets or raw transcripts in the log. This is separate from the approval ledger.

## Next integration steps

1. Add stable session-grouped split manifests to `verify --init` and refuse
   candidate promotion when mandatory held-out evaluation is absent.
2. Feed candidate screening from actual verifier results, with comparable
   cost and correctness metrics, rather than manually assembled aggregates.
3. Surface hypothesis history to proposal generation without leaking held-out tasks.
4. Add multiple-candidate comparison with correction for selection bias.
5. Only then consider bounded exploratory proposals, still compiled to RunTune's
   narrowest typed artifact and subject to human approval.

No production promotion policy has been changed by this patch.
