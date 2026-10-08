"""One verify run's evidence, read from the host's own record of it.

Each host's parser (hosts/<host>.py) reads its event stream into one RunRecord: every
tool call (renamed to a shared vocabulary: Bash, Read, Write, Edit, Skill, Agent, ...),
every result, what the host reports loading (skills, MCP servers), and the final answer.
The shims and the hook add two logs of their own: what the run tried to reach
(`effects.jsonl`) and what was denied (`hook.jsonl`). Nothing here trusts the agent's
account of what it did.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


def _jsonl(path: str) -> list:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return json.dumps(content)


@dataclass
class RunRecord:
    run_dir: str
    started: bool = False             # the host came up and said so
    finished: bool = False            # it reached a normal end
    end_reason: str = ""              # why it did not, in the host's words
    model: str | None = None
    skills: list | None = None        # skills the host reports loading; None: it does not report them
    mcp: list | None = None           # MCP servers it reports; None: it does not report them
    turns: int | None = None
    cost_usd: float = 0.0
    tokens: dict = field(default_factory=dict)        # for hosts that report tokens and not cost
    final: str = ""
    uses: list = field(default_factory=list)          # [{id, name, input}]
    results: dict = field(default_factory=dict)       # tool call id -> text
    effects: list = field(default_factory=list)
    hook: list = field(default_factory=list)
    init: dict = field(default_factory=dict)          # raw host start event, where there is one
    result: dict = field(default_factory=dict)        # raw host end event, where there is one

    @property
    def answer(self) -> str:
        return (self.final or "").strip()

    @property
    def bash(self) -> list:
        return [u["input"].get("command", "") for u in self.uses if u["name"] == "Bash"]

    @property
    def unmocked(self) -> list:
        return [e for e in self.effects if e.get("kind") == "unmocked"]

    @property
    def denied(self) -> list:
        return [h for h in self.hook if h.get("decision") == "deny"]


def harness_logs(r: RunRecord) -> RunRecord:
    r.effects = _jsonl(os.path.join(r.run_dir, "state", "effects.jsonl"))
    r.hook = _jsonl(os.path.join(r.run_dir, "state", "hook.jsonl"))
    return r


def skill_reads(r: RunRecord, skill_dirs: tuple) -> None:
    """Hosts without a Skill tool load a skill by reading its SKILL.md. Record that read as
    a Skill use too, so {"skill": name} measures the same thing on every host."""
    rx = re.compile(r"(?:^|[\s/'\"])(?:" + "|".join(re.escape(d.strip("/")) for d in skill_dirs)
                    + r")/([\w.-]+)/SKILL\.md\b")
    extra = []
    for u in r.uses:
        if u["name"] in ("Bash", "Read"):
            for name in set(rx.findall(json.dumps(u["input"]).replace("\\/", "/"))):
                extra.append({"id": f"{u['id']}#skill", "name": "Skill", "input": {"skill": name}, "via": u["name"]})
    r.uses += extra


def load(run_dir: str) -> RunRecord:
    """Claude Code's stream-json (kept for callers that predate hosts/)."""
    from .hosts.claude import parse
    return parse(run_dir)


# ------------------------------------------------------------------ measures
def matches(rec: RunRecord, spec: dict | None) -> int:
    """How many tool calls in the run match a measure spec. A spec names exactly one of:
        {"bash": <regex over the command>}
        {"skill": <skill name>}             the Skill tool invoked with it
        {"agent": <agent type>}             the Agent/Task tool spawning it
        {"tool": <tool name>}               any call to that tool
    """
    if not spec:
        return 0
    n = 0
    for u in rec.uses:
        inp = u["input"]
        if "bash" in spec:
            n += u["name"] == "Bash" and bool(re.search(spec["bash"], inp.get("command", ""), re.S))
        elif "skill" in spec:
            n += u["name"] == "Skill" and re.sub(r"^[\w-]+:", "", str(inp.get("skill", ""))) == spec["skill"]
        elif "agent" in spec:
            n += u["name"] in ("Agent", "Task") and inp.get("subagent_type") == spec["agent"]
        elif "tool" in spec:
            n += u["name"] == spec["tool"]
    return n


def correct(rec: RunRecord, expect: dict | None) -> bool | None:
    """{"contains": s} | {"regex": r} | {"not_contains": s} over the final answer. None: no expectation."""
    if not expect:
        return None
    a = rec.answer
    if "contains" in expect:
        return expect["contains"] in a
    if "regex" in expect:
        return bool(re.search(expect["regex"], a, re.S))
    if "not_contains" in expect:
        return expect["not_contains"] not in a
    raise ValueError(f"unknown expectation {expect}")


def peeked(rec: RunRecord, run_dir: str, real_root: str) -> list:
    """Tool calls that reached outside the run's project copy: into the harness (state/,
    bin/, the RUNTUNE_VERIFY_* variables) or into the REAL repository, which the run must
    not touch. A run that did either answered from a world production never has."""
    run_dir = os.path.realpath(run_dir)
    marks = re.compile(re.escape(run_dir) + r"(?!/proj(?:/|\b))")
    real = re.compile(re.escape(os.path.realpath(real_root)) + r"(?:/|\b)")
    out = []
    for u in rec.uses:
        blob = json.dumps(u["input"])
        if marks.search(blob) or "RUNTUNE_VERIFY_" in blob:
            out.append(f"{u['name']}: reached the verify harness: {blob[:120]}")
        elif real.search(blob):
            out.append(f"{u['name']}: used the real repository path: {blob[:120]}")
    return out
