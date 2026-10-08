# Verify: run a proposal before anyone applies it

RunTune's other checks are observational. The replay gate matches a proposal against recorded
attempts without executing anything, and `review` measures an artifact on the runs that follow
its merge. Neither can catch a skill that names a command that does not exist (RunTune shipped
one) or a skill agents never act on, before a person has approved it.

`runtune verify` executes the proposal. It runs the host headless on tasks you write, twice:
once against the repository as it is (**control**) and once with the drafted skill or sub-agent
in place (**treatment**), and compares what the agent did.

```bash
runtune stage <id>                     # as before: writes the drafts
runtune verify <id> --static           # free: do the commands it names exist? does the agent file parse?
runtune verify <id> --init             # writes .runtune/verify/<id>/tasks.json for you to fill in
runtune verify <id>                    # control vs treatment, Fisher-tested
runtune apply <id> --approve alice --eval .runtune/verify/<id>/result-<ts>.json
runtune verify --selftest              # prove the containment on this machine first
```

Supported: **capabilities and sub-agents.** Constraints already have the replay gate; routes
need a model eval (see [Route evals](#route-evals)).

## Hosts

The task file's `host` picks the agent that runs. `--init` sets it from where the artifact's
target is (`.agents/skills` is Codex's), or from `--host`. `--static` fails when the chosen
host would never load the target: a skill written to `.claude/skills` does not reach Codex.

| host | runs | containment | proven here |
|---|---|---|---|
| `claude` | `claude -p --output-format stream-json` | Claude Code's sandbox (`strictAllowlist`), shims, hook | selftest and end-to-end |
| `codex` | `codex exec --json` in a throwaway `CODEX_HOME` and `HOME` | a Seatbelt permission profile: reads of credentials, `.env`, `.runtune` and both Codex homes denied, the real repository read-only, network off, shims, hook | selftest and end-to-end |
| `cursor` | `agent -p --output-format stream-json` in a throwaway `HOME` | `sandbox.json` with `readBoundary: "workspace"` and network deny, permission denies, hook (`failClosed`) | built from Cursor's documentation; not yet run against the CLI |
| `antigravity` | `agy -p --output-format stream-json` in a throwaway `HOME` | the terminal sandbox, `read_file` denies for the file tools, hook | built from Antigravity's documentation; not yet run against the CLI |

The Cursor and Antigravity runners follow each vendor's documented event stream, sandbox,
hook and configuration formats, checked against what each product's own agent reported about
itself. Three things are documented loosely enough that only a run will settle them: the shape
of a shell result in Cursor's stream, whether Cursor's `--force` turns its sandbox off, and
whether Antigravity's hooks fire in print mode. Their first `--selftest` answers all three. If
you run one, an issue with the output is welcome.

Verify refuses Codex, Cursor and Antigravity runs until `runtune verify --selftest --host
<host>` has passed for the CLI version installed (the result is kept under
`~/.cache/runtune-verify/selftest/`). Their containment is configuration, and configuration a
new version stops honoring fails silently: Cursor's headless mode ignored `sandbox.json` for
several releases in 2026.

What differs by host, and is written into each result's `limits`:

- **Skill loading.** Claude Code reports which skills loaded. Codex does not, so verify renders
  Codex's prompt for each run with `codex debug prompt-input` (no model call) and checks the
  skill is listed. Cursor and Antigravity report nothing; there a treatment run counts whether
  or not the skill loaded, and `{"skill": name}` counts reads of its `SKILL.md`.
- **Cost.** Only Claude Code reports cost. The others report tokens, which are recorded.
- **User configuration.** Each non-Claude run gets its own home directory, so the user's MCP
  servers, plugins, hooks, skills and rules stay out. Codex needs this most: `~/.codex/config.toml`
  can hold MCP credentials in plaintext. Codex authenticates with `CODEX_API_KEY`, or through a
  link to the real `~/.codex/auth.json`. Cursor uses its CLI's own login (`agent login`, kept
  in the macOS Keychain, which a different `HOME` does not hide) or `CURSOR_API_KEY`.
  Antigravity keeps its login in files under `~/.gemini`, so a run needs `GEMINI_API_KEY`.
- **Project configuration.** The run does not trust the project for Codex, so its
  `.codex/config.toml`, rules and hooks do not load; `AGENTS.md` and `.agents/skills` do.
  Cursor loads the project's `.claude/settings.json` hooks, as it does in production. A
  repository's `.cursor/sandbox.json` outranks the user-level one, and `.cursor/mcp.json` and
  `.agents/mcp_config.json` start MCP servers, so verify removes those from each run's copy
  and says so in the result.
- **Environment.** Codex strips `CODEX_*` and `OPENAI_*` from the shell. Cursor and
  Antigravity document no filter, so their API key is visible to the agent's shell.
- **Hooks are a guardrail, not the boundary** on Codex, Cursor and Antigravity: each documents
  tool paths that skip them or failure modes that let a call through. The sandbox is the
  boundary; the hook adds the write-path and PATH checks.

## The task file

`--init` writes the task file. Where it can, it drafts the tasks from the sessions behind the
evidence (see [Drafted tasks](#drafted-tasks)); otherwise it leaves a placeholder for you:

```json
{
  "artifact": "cap-inline-toolkit-316032da",
  "runs": 4,
  "model": "sonnet",
  "measure": {
    "new": {"bash": "(?:^|[\\s;&|(])(?:\\./)?(?:scripts/toolkit\\ db)\\b"},
    "old": {"bash": "(?:python3?\\s+(?:-c|-\\s*<<)|<<).*(?:from\\s+toolkit\\s+import|import\\s+toolkit\\b)"}
  },
  "exercise": ["python3 -c \"... from toolkit import db ...\""],
  "tasks": [
    {"name": "order-count", "prompt": "How many orders are in the warehouse? Reply with just the number.",
     "expect": {"regex": "\\b137\\b"}}
  ],
  "mocks": [], "intercept": [], "passthrough": [], "repo_modules": ["toolkit"],
  "live": {"network": [], "env": [], "read": []}
}
```

- **`measure`** is what the artifact should make agents do (`new`) and what it replaces
  (`old`). Each side is `{"bash": regex}`, `{"skill": name}`, `{"agent": type}` or
  `{"tool": name}`. For an adoption-gap capability, `--init` fills both from the proposal:
  the CLI commands seen succeeding and the companion nudge's pattern.
- **`exercise`** lists recorded samples of the behavior the tasks should provoke. Write tasks
  a real user would type that lead there. Don't name the skill.
- **`expect`** checks the final answer: `contains`, `regex` or `not_contains`.
- A task file with a `<placeholder>` left in it is refused.

### Drafted tasks

Events keep each command and its outcome, not the conversation around it. A capability's
samples also keep their session, so `--init` can read each sample's turn back from the host's
own transcript: what the person asked, what the command printed, and what the agent
answered. That works for all four hosts while the transcript exists. Claude Code deletes
transcripts after `cleanupPeriodDays` (30 by default); Codex, Cursor and Antigravity keep
theirs. Nothing read this way is added to the event store; it goes only into the task file.

A turn becomes a draft only if the request led the agent to the old path directly: the call is
among the first three in its turn, the turn has at most 12 calls, and a later request in the
session does not open as a continuation ("yes, redeploy", "promote as is"). Each sampled
session is searched for its most direct such turn. A draft looks like this:

```json
{"name": "seen-1", "prompt": "How many orders came in last week?",
 "expect": {"regex": "(?<![\\d.,])137(?![\\d])"}, "reviewed": false,
 "from_evidence": {"source": "claude", "session": "…", "ts": "…", "command": "…",
                   "output": "…", "answer": "…", "call": "1 of 2 in its turn",
                   "request": "1 in its session",
                   "expect_basis": "a number in both the command's output and the answer"}}
```

`expect` is a guess: a number that appears both in the command's output and in a short final
answer, and not in the prompt. Without one it is left as a placeholder. A file with any task
still `"reviewed": false` is refused. Read the prompt and the expectation against
`from_evidence`, fix or delete the task, then set `reviewed` to true. The answer came from the
live system on the day the session ran: in strict mode the run cannot reach that system, so a
drafted task needs mocks, a local fixture, or `--live`.

Expect few drafts from conversational use. On one real repository (13,136 Claude Code events,
three capability candidates, 35 sampled sessions) the filters left 4 drafts and none had a
guessable answer: most inline calls happened deep inside long work, and most direct requests
were follow-ups to an earlier exchange. Drafting saves the most where agents are given
self-contained requests.

## What a verdict means

A run counts when it finished, loaded what it should have (the treatment's skill, no MCP
servers), and stayed inside the case (below). Valid runs are pooled per arm.

| verdict | when |
|---|---|
| `pass` | treatment took the new path significantly more often than control (Fisher, p < 0.05) and was not less correct. With no `measure`: every treatment run correct and none worse than control, which shows no harm and not an effect |
| `no-effect` | treatment took the new path no more often than control |
| `fail` | static checks failed (no runs are made), or treatment answered significantly fewer tasks correctly |
| `inconclusive` | the difference did not reach significance, or fewer than half of an arm's runs were valid |

Before spending anything, verify prints the best p the run count can reach. Three runs per arm
cannot pass: 3/3 against 0/3 is p = 0.10. Four is the minimum (p = 0.029), and that only
with perfect separation.

`apply --eval` accepts a verify result only when its verdict is `pass`, it verified this artifact, the
draft has not changed since, and the target file is the version it ran against. Any `--eval`
given for a capability or sub-agent is checked; a file that is not a verify result is refused.
Add kinds to `require_verify` in `.runtune/authority.json` to make verification mandatory:

```json
"require_verify": ["capability", "subagent"]
```

Every verify is written to the ledger with its verdict, mode and cost.

## Route evals

Routes are not run by `verify`: whether a model is good enough for a mode is a question for
the eval harness that scores that mode. What `apply --eval` checks is that the eval is about
this change. Admitting a model to a mode, and re-clearing a stale one, both need a result
in this shape, written by your eval harness:

```json
{"schema": "runtune.route-eval/1", "mode": "extract",
 "candidate": "qwen/cheap", "incumbent": "deepseek/v4-flash",
 "verdict": "pass", "cases": 40, "metric": "field F1 vs gold",
 "candidate_score": 0.91, "incumbent_score": 0.90, "non_inferiority_margin": 0.02,
 "created": "2026-10-08T12:00:00Z", "harness": "evals/extract.py"}
```

It is refused when the mode, the candidate or the incumbent differs from the proposal; when
the verdict is not `pass`; when fewer than `route_eval.min_cases` cases were scored (default
20); when it is older than `route_eval.max_age_days` (default 30); when either score is
missing; or when it passes a candidate that scored below the incumbent by more than its
declared `non_inferiority_margin`. To re-clear a stale mode, set `candidate` to the model the
mode already runs and omit the incumbent. The eval's sha256 is written into `routes.json`
next to the model it cleared, and into the ledger, so a later edit to the file is visible.

## Containment

Each run gets its own copy of the repository, so the real `CLAUDE.md`, project hooks, skills
and agents load, with the drafted artifact overlaid in the treatment arm. The copy is a
copy-on-write clone (`cp -c` on APFS, `--reflink` on Linux): on a 90,000-file repository it
takes about 10 seconds and almost no disk. Where the filesystem cannot clone, the run falls
back to a farm of symlinks and the result says so, because tools that do not follow links see
a different tree: `find dir -type f` does not descend into a symlinked directory, and in the
first real run of this feature one control agent saw an empty project and answered wrong for
that reason. `.git`, `.runtune` (which holds the expected answers) and `.env*` are left out.
Nothing is written to the real repository.

Three layers, each covering what the others cannot see:

1. **Shims** on PATH for `python3`, `curl`, `psql`, `gh`, `ssh`, `git` and other tools that
   reach outside. A mock answers a matching call. In strict mode a repository script or module
   with no mock is refused, and so is a network tool; nothing runs. Repository executables
   called by path (`scripts/toolkit`) are routed through the shim when listed in `intercept`.
2. **A PreToolUse hook**, fail-closed. Write and Edit run outside the OS sandbox, so every
   write path is resolved through any symlinks (the fallback farm is made of them) and must
   stay in the run directory. Bash that names an interpreter by absolute path
   or rewrites `PATH` is denied, because both skip the shims.
3. **Claude Code's OS sandbox**, set strict. In `bypassPermissions` mode the sandbox allows
   hosts outside the allowlist unless `strictAllowlist` is on, so verify sets it. Unsandboxed
   retries are off, startup fails if the sandbox is unavailable, and credential paths
   (`~/.ssh`, `~/.aws`, `~/.config`, the repository's `.env*`, …) are unreadable.

The environment is an allowlist, so no variable from your shell reaches a run unless the task
file names it. `RUNTUNE_HOME` points into the run, so a verify run never records itself into
your own evidence. A run that reaches outside its case (an unmocked call, a denied bypass, a
path into the real repository or into the harness) is excluded from the counts and reported.

`--selftest [--host H]` checks all of this on the current machine. A headless agent attempts
each escape against a throwaway repository with three planted secrets, and each probe is
judged from the logs, not from what the agent says. Models decline steps that look like
exfiltration: on its first selftest Codex skipped every one that read `.env` or wrote outside
its folder and replied "DONE". A probe the model skips is then run directly under the host's
sandbox with no model (`codex sandbox` with the run's own profile), or, for file writes, by
handing the run's hook a write in that host's format. Those results are labelled "direct".

### Live mode

Some tasks can only be answered from a real system, such as a database the CLI exists to
query. `--live --reason "<why>"` lets runs reach the task file's `live.network` domains, see
the `live.env` variables and read the `live.read` paths, and lets unmocked repository scripts
run for real. The reason is recorded with the result and in the ledger.

In live mode the hook still confines writes to the run directory. **A script that writes
through an allowed domain, such as an API POST, is not blocked.** Use read-only credentials.

## Limits

- **The tasks are yours, so the verdict is about them.** A pass on two questions is evidence
  about those two questions, including when they were drafted from real sessions.
- **An end-to-end run cannot see a defect the model compensates for.** In one measured case
  both Opus and Haiku overrode a known-wrong parser in every run, so the end-to-end case passed
  on the broken version. A pass means the system behaved, not that every layer is right; a
  unit test is the guard for a deterministic part.
- **Cost.** Every run is a real headless session: tasks × 2 arms × runs. The total is in the
  result, at list price.
