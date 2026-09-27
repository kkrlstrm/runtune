"""The one record every deriver emits.

A candidate is a PROPOSAL, never an action. It carries everything a reviewer
needs to decide in about a minute, and everything the promoter needs to refuse
it if the reviewer is wrong:

    claim          one sentence: what the evidence says
    tier           how much the evidence is allowed to claim (evidence.tiers)
    ladder         the compile order, AutoRefine-style: constraint -> skill ->
                   subagent. Each rung records whether it closes and why not, so
                   "why is this a subagent and not a skill" has a written answer.
    correction     the attempts this artifact exists to change
    preservation   the near-miss attempts it must NOT disturb. An artifact with no
                   preservation evidence has never been checked for collateral.
    gates          every admission check, including ones that were switched off —
                   a suppressed gate still runs and its verdict is recorded; only
                   its power to block is removed.
    direction      tighten | widen | neutral. The authority model keys on this:
                   the learner may propose a widening, never apply one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

KINDS = ("constraint", "capability", "subagent", "route")
TIGHTEN, WIDEN, NEUTRAL = "tighten", "widen", "neutral"


@dataclass
class Gate:
    name: str
    passed: bool
    reason: str = ""
    suppressed: bool = False


@dataclass
class Candidate:
    kind: str
    key: str                     # stable identity of the underlying cluster
    title: str
    claim: str
    tier: str
    direction: str = NEUTRAL
    sources: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)
    ladder: list = field(default_factory=list)      # [(rung, closes, why)]
    correction: list = field(default_factory=list)
    preservation: list = field(default_factory=list)
    gates: list = field(default_factory=list)
    proposal: dict = field(default_factory=dict)
    requires: list = field(default_factory=list)    # e.g. ["human-review", "eval"]
    caveats: list = field(default_factory=list)

    @property
    def id(self) -> str:
        h = hashlib.sha256(f"{self.kind}|{self.key}".encode()).hexdigest()[:8]
        slug = "".join(c if c.isalnum() else "-" for c in self.key.lower())[:40].strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return f"{self.kind[:3]}-{slug}-{h}"

    @property
    def admitted(self) -> bool:
        return all(g.passed or g.suppressed for g in self.gates)

    def gate(self, name: str, passed: bool, reason: str = "", suppressed: bool = False):
        self.gates.append(Gate(name, bool(passed), reason, suppressed))
        return passed

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        d["admitted"] = self.admitted
        return d

    def digest(self) -> str:
        body = json.dumps({k: v for k, v in self.to_dict().items() if k != "id"},
                          sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()
