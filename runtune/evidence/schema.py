"""One evidence stream, five producers.

Claude Code, Codex, Cursor, Antigravity and OpenRouter record different things in different shapes.
RunTune reduces all of them to four record types so every deriver and every
measurement runs on the same footing:

    Event        one settled ATTEMPT at something — a tool call, a shell command,
                 a model request. It succeeded or it failed. Unsettled attempts
                 (pending, orphaned, still running) are counted in coverage and
                 never enter a rate: an attempt with no outcome is not a success.

    Invocation   one agent's life — a sub-agent, a Codex thread, a root session.
                 The unit the skilled-subagent deriver reasons over.

    Message      one cross-session message: one session telling another what changed.

    WorkflowRun  one Workflow fan-out run, as the harness recorded it.

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
    billing: str | None = None  # "api" (metered per token) | "subscription" | None (unknown)

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
    # What the agent was asked to do. A hook payload carries none of it; it comes from the
    # agent's own transcript and the `.meta.json` Claude Code writes beside it.
    description: str | None = None   # the Agent call's description, or a workflow agent's label
    prompt: str | None = None        # the task it was given: redacted, truncated
    parent_call: str | None = None   # the parent's Agent tool_use id: an exact link, not a type match
    workflow_run: str | None = None  # wf_* run id when a Workflow spawned it
    workflow_name: str | None = None
    workflow_phase: str | None = None
    result_chars: int | None = None  # length of what it returned; None = not recorded
    recovered: bool = False          # rebuilt from disk after the live recorder missed it

    @property
    def task(self) -> str | None:
        """A stable name for the job, so agents doing the same work group together.

        A workflow agent is named by its workflow and phase; an Agent-tool agent by its
        description, with a `verify:<item>`-style label reduced to its prefix, because the
        part after the colon names the item, not the job."""
        label = (self.description or "").strip()
        head = label.split(":", 1)[0].strip().lower() if ":" in label else ""
        if self.workflow_name:
            step = (self.workflow_phase or head or "").strip().lower()
            return f"{self.workflow_name}/{step}" if step else self.workflow_name
        if head:
            return head
        return label.lower()[:60] or None


@dataclass(slots=True)
class Message:
    """One cross-session message: a Claude Code session telling another what changed.

    The sending half is a SendMessage tool call; the receiving half fires no hook and
    exists only in the receiver's transcript. Both ends share Claude Code's msg_id, so
    `matched` means both transcripts were read."""
    source: str
    msg_id: str
    at: datetime
    sender: str | None              # session id
    receiver: str | None            # session id
    addressed_to: str | None = None
    sender_name: str | None = None
    first_line: str = ""            # redacted: what the receiving user sees in the preview
    body: str = ""                  # redacted, truncated
    body_chars: int = 0
    delivery: str | None = None     # new_turn | mid_turn | None (receipt not recorded)
    notify_when_idle: bool = False
    send_result: str | None = None
    sender_inferred: bool = False   # sender resolved from the inbox-socket table, not the msg_id join

    @property
    def matched(self) -> bool:
        return bool(self.sender and self.receiver)


@dataclass(slots=True)
class WorkflowRun:
    """One Workflow tool run. The tool returns at launch; the run's record lands later."""
    source: str
    run_id: str
    session: str
    name: str | None
    status: str | None
    started: datetime | None
    duration_ms: int | None = None
    agents_declared: int | None = None
    tokens_declared: int | None = None


@dataclass
class Coverage:
    source: str
    first: datetime | None = None
    last: datetime | None = None
    settled: int = 0
    unsettled: int = 0          # pending / orphaned / running — excluded from rates
    gaps: list = field(default_factory=list)   # [(start_date, end_date, days)]
    notes: list = field(default_factory=list)
    summary: str = ""           # a source that is not a stream of attempts says what it holds

    def line(self) -> str:
        if self.summary:
            return f"{self.source}: {self.summary}"
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
    regression: dict | None = None          # latest regression run (sources/regression.py)
    spend: list = field(default_factory=list)  # OpenRouter daily activity rows
    # source -> the only tools that recorder captures (absent = it sees everything)
    capture_allowlist: dict = field(default_factory=dict)
    messages: list = field(default_factory=list)       # cross-session messages (Message)
    workflow_runs: list = field(default_factory=list)  # WorkflowRun

    def extend(self, other: "Corpus") -> "Corpus":
        self.events.extend(other.events)
        self.invocations.extend(other.invocations)
        self.coverage.update(other.coverage)
        if other.route_policy is not None:
            self.route_policy = other.route_policy
        if other.regression is not None:
            self.regression = other.regression
        self.spend.extend(other.spend)
        self.capture_allowlist.update(other.capture_allowlist)
        self.messages.extend(other.messages)
        self.workflow_runs.extend(other.workflow_runs)
        return self

    def by(self, key) -> dict:
        out = defaultdict(list)
        for e in self.events:
            out[key(e)].append(e)
        return out

    def sources(self) -> list[str]:
        return sorted({e.source for e in self.events} | set(self.coverage))

    def finalize(self) -> "Corpus":
        """Fill coverage gaps from the events themselves, and join what was recorded in pieces."""
        self.messages = merge_messages(self.messages)
        self.workflow_runs = list({(w.source, w.run_id): w for w in self.workflow_runs}.values())
        names = {(w.source, w.run_id): w.name for w in self.workflow_runs}
        for i in self.invocations:
            if i.workflow_run and not i.workflow_name:
                i.workflow_name = names.get((i.source, i.workflow_run))
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


def merge_messages(messages: list) -> list:
    """One Message per msg_id. A transcript recorder sees each message twice, once from
    each end; the sender's record carries the sender and what it addressed, the receiver's
    carries the receiver and how it was delivered. Non-empty fields win, earliest time wins."""
    out: dict = {}
    for m in messages:
        k = (m.source, m.msg_id)
        if k not in out:
            out[k] = m
            continue
        a = out[k]
        for f in ("sender", "receiver", "addressed_to", "sender_name", "delivery", "send_result"):
            if not getattr(a, f) and getattr(m, f):
                setattr(a, f, getattr(m, f))
        if not a.first_line and m.first_line:
            a.first_line, a.body, a.body_chars = m.first_line, m.body, m.body_chars
        a.notify_when_idle = a.notify_when_idle or m.notify_when_idle
        if m.at and (a.at is None or m.at < a.at):
            a.at = m.at
    return list(out.values())
