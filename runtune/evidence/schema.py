"""One evidence stream, five producers.

Claude Code, Codex, Cursor, Antigravity and OpenRouter record different things in different shapes.
RunTune reduces all of them to two record types so every deriver and every
measurement runs on the same footing:

    Event        one settled ATTEMPT at something — a tool call, a shell command,
                 a model request. It succeeded or it failed. Unsettled attempts
                 (pending, orphaned, still running) are counted in coverage and
                 never enter a rate: an attempt with no outcome is not a success.

    Invocation   one agent's life — a sub-agent, a Codex thread, a root session.
                 The unit the skilled-subagent deriver reasons over.

and one record of what the evidence could NOT see (`Coverage`), because a
derivation run over a window with a two-week hole in it must say so.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


@dataclass(slots=True)
class Event:
    source: str                 # "claude" | "codex" | "cursor" | "antigravity" | "openrouter" | "jsonl"
    session: str
    ts: datetime
    kind: str                   # "tool" | "model"
    surface: str                # tool name, or OpenRouter mode
    shape: str                  # clustering key (see evidence.shapes)
    ok: bool
    actor: str = "root"         # sub-agent type, Codex subagent type, or OR caller
    invocation: str | None = None
    text: str = ""              # command / label — already redacted and truncated
    error: str = ""             # normalized error signature, failures only
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    duration_ms: int | None = None
    inline_target: str | None = None
    chars: int = 0              # length of the original command, before truncation

    @property
    def week(self) -> str:
        y, w, _ = self.ts.isocalendar()
        return f"{y}-W{w:02d}"


@dataclass(slots=True)
class Invocation:
    source: str
    invocation: str
    session: str
    agent_type: str
    started: datetime | None
    ended: datetime | None
    status: str                 # completed | orphaned | pending | failed
    model: str | None = None
    tokens_out: int = 0
    tokens_reread: int = 0      # cache reads: context re-read every turn
    token_verified: bool = False
    tools: Counter = field(default_factory=Counter)
    failures: int = 0


@dataclass
class Coverage:
    source: str
    first: datetime | None = None
    last: datetime | None = None
    settled: int = 0
    unsettled: int = 0          # pending / orphaned / running — excluded from rates
    gaps: list = field(default_factory=list)   # [(start_date, end_date, days)]
    notes: list = field(default_factory=list)

    def line(self) -> str:
        if not self.first:
            return f"{self.source}: no data"
        s = (f"{self.source}: {self.settled:,} settled attempts "
             f"{self.first:%Y-%m-%d} → {self.last:%Y-%m-%d}")
        if self.unsettled:
            s += f"; {self.unsettled:,} unsettled excluded from rates"
        for a, b, d in self.gaps:
            s += f"; GAP {a} → {b} ({d}d with no data)"
        return s


def find_gaps(days_with_data: set[date], min_gap: int = 4) -> list:
    """Runs of >= min_gap consecutive empty days inside the observed span.

    A recorder that dies quietly produces exactly this signature, and a
    before/after that straddles it compares a real window with an empty one.
    """
    if not days_with_data:
        return []
    ds = sorted(days_with_data)
    out = []
    for a, b in zip(ds, ds[1:]):
        gap = (b - a).days - 1
        if gap >= min_gap:
            out.append(((a + timedelta(days=1)).isoformat(), (b - timedelta(days=1)).isoformat(), gap))
    return out


@dataclass
class Corpus:
    events: list = field(default_factory=list)
    invocations: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    route_policy: dict | None = None        # parsed allowlist (routes.json shape)
    spend: list = field(default_factory=list)  # OpenRouter daily activity rows
    # source -> the only tools that recorder captures (absent = it sees everything)
    capture_allowlist: dict = field(default_factory=dict)

    def extend(self, other: "Corpus") -> "Corpus":
        self.events.extend(other.events)
        self.invocations.extend(other.invocations)
        self.coverage.update(other.coverage)
        if other.route_policy is not None:
            self.route_policy = other.route_policy
        self.spend.extend(other.spend)
        self.capture_allowlist.update(other.capture_allowlist)
        return self

    def by(self, key) -> dict:
        out = defaultdict(list)
        for e in self.events:
            out[key(e)].append(e)
        return out

    def sources(self) -> list[str]:
        return sorted({e.source for e in self.events} | set(self.coverage))

    def finalize(self) -> "Corpus":
        """Fill coverage gaps from the events themselves."""
        per = defaultdict(set)
        for e in self.events:
            per[e.source].add(e.ts.date())
        for src, days in per.items():
            cov = self.coverage.setdefault(src, Coverage(src))
            evs = [e for e in self.events if e.source == src]
            cov.settled = len(evs)
            cov.first = min(e.ts for e in evs)
            cov.last = max(e.ts for e in evs)
            cov.gaps = find_gaps(days)
        return self
