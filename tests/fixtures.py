"""Synthetic corpora with known answers. Every number a test asserts is set here."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from runtune.evidence import shapes
from runtune.evidence.schema import Corpus, Event, Invocation

T0 = datetime(2026, 8, 1, 9, tzinfo=timezone.utc)


def ev(source="claude", session="s1", day=0, surface="Bash", text="", ok=True, error="",
       model="claude-opus-5", kind="tool", usd=0.0, actor="root", invocation=None, minute=0):
    shape = surface if kind == "model" else shapes.command_shape(surface, text)
    return Event(source=source, session=session, ts=T0 + timedelta(days=day, minutes=minute), kind=kind,
                 surface=surface, shape=shape, ok=ok, actor=actor, invocation=invocation, text=text,
                 error="" if ok else shapes.normalize_error(error), model=model, usd=usd,
                 inline_target=None if kind == "model" else shapes.inline_target(text), chars=len(text))


def failing_psql() -> Corpus:
    """`psql` fails 60% of the time across 4 sessions on two hosts; `git status` always works."""
    c = Corpus()
    for i in range(10):
        src = "codex" if i % 3 == 0 else "claude"
        surf = "exec" if src == "codex" else "Bash"
        c.events.append(ev(src, f"s{i % 4}", day=i, surface=surf, text="psql -c 'select 1'",
                           ok=i >= 6, error="psql: error: connection to server on socket failed"))
    for i in range(30):
        c.events.append(ev("claude", f"s{i % 5}", day=i % 10, text="git status"))
    # a one-session retry loop: 6 failures, all in one session
    for i in range(6):
        c.events.append(ev("claude", "loop", day=1, text="make deploy", ok=False, error="make: *** [deploy] Error 1",
                           minute=i))
    for i in range(3):
        c.events.append(ev("claude", "loop", day=2, text="make deploy"))
    return c.finalize()


def inline_ttt(cli_after_day: int | None = 20) -> Corpus:
    c = Corpus()
    snippet = ('python3 -c "import sys; sys.path.insert(0,\'scripts/lib\'); from ttt import db, clickup; '
               'print(db.query_master(\'select 1\')); clickup.tasks_for(x)"')
    for i in range(40):
        c.events.append(ev("claude", f"s{i % 12}", day=i, text=snippet))
    if cli_after_day is not None:
        for i in range(15):
            c.events.append(ev("claude", f"s{i % 6}", day=cli_after_day + i, text="scripts/ttt db master 'select 1'"))
    # standard-library-only snippets must NOT become a capability
    for i in range(40):
        c.events.append(ev("claude", f"s{i % 12}", day=i, text='python3 -c "import json, sys; print(1)"'))
    return c.finalize()


def fanouts() -> Corpus:
    c = Corpus()
    for f in range(6):
        for a in range(8):
            tools = Counter({"Bash": 5}) if a % 2 else Counter({"Bash": 3, "WebSearch": 1})
            c.invocations.append(Invocation(
                source="claude", invocation=f"f{f}a{a}", session=f"sess{f}", agent_type="workflow-subagent",
                started=T0 + timedelta(days=f * 5), ended=T0 + timedelta(days=f * 5, minutes=3),
                status="completed", tools=tools, tokens_out=100, tokens_reread=30000, token_verified=True))
    for i in range(12):
        c.invocations.append(Invocation(
            source="claude", invocation=f"db{i}", session=f"q{i}", agent_type="db-reader",
            started=T0 + timedelta(days=i), ended=None, status="completed", tools=Counter({"Bash": 4}),
            tokens_out=100, tokens_reread=5000, token_verified=True))
    return c.finalize()


def routing() -> Corpus:
    c = Corpus()
    c.route_policy = {"modes": {
        "extract": {"model": "deepseek/v4-flash", "verified_date": "2026-07-01"},
        "dial": {"model": "google/flash-lite", "verified_date": "2026-08-10"},
        "idle-mode": {"model": "qwen/qwen3", "verified_date": "2026-03-01"},
    }, "retired": {}, "staleness_warn_days": 120, "path": "inline"}
    for i in range(60):
        c.events.append(ev("openrouter", "job", day=i % 20, surface="extract", kind="model",
                           model="deepseek/v4-flash", ok=i % 30 != 0, usd=0.001, actor="extract.py:run"))
    for i in range(30):  # a cheaper, equally reliable challenger inside the same mode
        c.events.append(ev("openrouter", "job", day=i % 20, surface="extract", kind="model",
                           model="qwen/cheap", ok=True, usd=0.0002, actor="bench.py:run"))
    for i in range(25):  # ran BEFORE `dial` was re-cleared on 08-10 -> history, not drift
        c.events.append(ev("openrouter", "job", day=i % 5, surface="dial", kind="model", model="openai/old", usd=0.01))
    for i in range(25):  # ran AFTER re-clearance on a different model -> drift
        c.events.append(ev("openrouter", "job", day=12 + i % 5, surface="dial", kind="model", model="openai/old",
                           usd=0.01))
    for i in range(22):
        c.events.append(ev("openrouter", "job", day=i % 5, surface="(no mode)", kind="model", model="x/rogue",
                           usd=0.002, ok=i % 2 == 0))
    c.spend = [{"usage_date": (T0 + timedelta(days=d)).date().isoformat(), "model": "z/unlogged",
                "requests": 40, "usage_usd": 0.5} for d in range(3)]
    return c.finalize()
