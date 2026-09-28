"""Where RunTune keeps its own state on a machine. One place, overridable by $RUNTUNE_HOME.

    ~/.runtune/
      events/YYYY-MM-DD.jsonl   what the hook and the recorders saw (portable format)
      spend/YYYY-MM-DD.jsonl    provider billing snapshots (OpenRouter activity)
      audit.jsonl               every enforcement verdict, hash-chained
      rules.json                machine-wide constraints (a project adds rules/runtune.rules.json)
      recorder-state.json       which transcripts, rollout files and Cursor sessions were already read
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def root() -> str:
    return os.path.expanduser(os.environ.get("RUNTUNE_HOME", "~/.runtune"))


def path(*parts: str) -> str:
    return os.path.join(root(), *parts)


def day_file(kind: str, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    d = path(kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{when:%Y-%m-%d}.jsonl")


def rule_files(cwd: str | None = None) -> list[str]:
    """Rulesets the hook enforces: $RUNTUNE_RULES (colon list) if set, else the
    project's rules/runtune.rules.json plus the machine-wide ~/.runtune/rules.json."""
    env = os.environ.get("RUNTUNE_RULES")
    if env:
        return [p for p in env.split(":") if p]
    out = [path("rules.json")]
    if cwd:
        out.insert(0, os.path.join(cwd, "rules", "runtune.rules.json"))
    return out
