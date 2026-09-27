"""Does a RunTune-derived capability change what agents do? A live three-arm test.

RunTune proposed two artifacts from 5,899 inline `ttt` programs:
  * capability  `use-ttt-cli` — a skill naming the CLI invocations seen succeeding
  * constraint  `nudge-inline-ttt` — its companion nudge, armed by RunTune's own hook

Arms (everything else identical — same repo, same CLAUDE.md, same global hooks):
  A  control          the repo as it is
  B  + skill          .claude/skills/use-ttt-cli/SKILL.md present
  C  + skill + nudge  B, plus `python3 -m runtune.hook` enforcing the nudge via --settings

Tasks are read-only questions whose answers live behind the `ttt` library, each
with a ground truth computed directly beforehand. Runs are headless Sonnet
sessions (`claude -p`), serial, arm order shuffled within each task x rep block.

Measured per run from the stream-json transcript: inline `ttt` programs vs CLI
calls, correctness, turns, wall time, cost as reported.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.expanduser(os.environ.get("AB_REPO", "~/kai-gtm-agents"))
RUNTUNE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(REPO, ".claude", "skills", "use-ttt-cli")
REPS = int(os.environ.get("AB_REPS", "2"))

ROUND = os.environ.get("AB_ROUND", "2")
# The task list names real clients, so it lives in a gitignored tasks.local.json;
# tasks.example.json shows the shape. Round 1's tasks had a cheaper path than the library
# (38 of 50 calls read config/clients.json directly), so round 2's can only be answered
# through the database or GitLab — the paths ttt exists for.
_TASKS_FILE = os.path.join(HERE, "tasks.local.json")
TASKS = [tuple(t) for t in json.load(open(_TASKS_FILE))[ROUND]] if os.path.exists(_TASKS_FILE) else []

INLINE = re.compile(r"(?:python3?\s+(?:-c|-\s*<<)|<<).*(?:from\s+ttt\s+import|import\s+ttt\b)", re.S)
CLI = re.compile(r"(?:^|[\s;&|(])(?:\./)?(?:scripts/)?ttt\s+[a-z]")


def install_skill(on: bool) -> None:
    if on and not os.path.exists(SKILL_DIR):
        shutil.copytree(os.path.join(HERE, "use-ttt-cli"), SKILL_DIR)
    if not on and os.path.exists(os.path.join(SKILL_DIR, ".ab-experiment")):
        shutil.rmtree(SKILL_DIR)


def settings_for(arm: str, tmp: str) -> str | None:
    if arm != "C":
        return None
    rules = os.path.join(HERE, "nudge.rules.json")
    cmd = (f"RUNTUNE_HOME={tmp}/rt RUNTUNE_RULES={rules} PYTHONPATH={RUNTUNE} "
           f"python3 -m runtune.hook --host claude")
    s = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}}
    p = os.path.join(tmp, "settings.json")
    with open(p, "w") as f:
        json.dump(s, f)
    return p


def run_one(arm: str, task: tuple, rep: int, tmp: str) -> dict:
    name, prompt, truth = task
    install_skill(arm in ("B", "C"))
    cmd = ["claude", "-p", prompt, "--model", "sonnet", "--output-format", "stream-json", "--verbose",
           "--max-turns", "15", "--allowedTools", "Bash,Read,Grep,Glob,Skill"]
    st = settings_for(arm, tmp)
    if st:
        cmd += ["--settings", st]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=600)
    wall = time.time() - t0
    inline = cli = bash = 0
    skill_used = False
    result = {}
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == "Skill" and "ttt" in json.dumps(block.get("input")):
                    skill_used = True
                if block.get("name") == "Bash":
                    c = (block.get("input") or {}).get("command", "")
                    bash += 1
                    inline += bool(INLINE.search(c))
                    cli += bool(CLI.search(c)) and not INLINE.search(c)
        elif obj.get("type") == "result":
            result = obj
    answer = (result.get("result") or "").strip()
    return {"arm": arm, "task": name, "rep": rep, "correct": truth in answer, "answer": answer[:120],
            "bash_calls": bash, "inline_ttt": inline, "cli_ttt": cli, "skill_invoked": skill_used,
            "turns": result.get("num_turns"), "cost_usd": result.get("total_cost_usd"),
            "wall_s": round(wall, 1), "session_id": result.get("session_id"), "exit": proc.returncode}


def main() -> int:
    if not TASKS:
        sys.exit("no tasks: copy tasks.example.json to tasks.local.json and fill it in")
    out = os.path.join(HERE, f"results-round{ROUND}.jsonl")
    tmp = tempfile.mkdtemp(prefix="ttt-ab-")
    blocks = [(t, r) for r in range(REPS) for t in TASKS]
    rng = random.Random(20260927)
    try:
        with open(out, "a") as f:
            for task, rep in blocks:
                arms = ["A", "B", "C"]
                rng.shuffle(arms)
                for arm in arms:
                    row = run_one(arm, task, rep, tmp)
                    f.write(json.dumps(row) + "\n")
                    f.flush()
                    print(json.dumps(row), flush=True)
    finally:
        install_skill(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
