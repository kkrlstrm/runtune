"""Evidence sources. Each returns a `Corpus`; `load` merges any combination."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from ..evidence.schema import Corpus, Coverage


def guarded(source: str, fn) -> Corpus:
    """Run one warehouse source. A table or column the warehouse lacks becomes a coverage
    note and a warning, not a traceback: one source's gap must not stop the others, and
    a source that was skipped must say so rather than read as "no failures"."""
    from . import pg
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - psycopg is an optional import
        miss = pg.missing(exc)
        if not miss:
            raise
        note = f"skipped: the warehouse has no {miss[0]} `{miss[1]}` (column contract: docs/WAREHOUSE.md)"
        print(f"runtune: warning — {source} {note}", file=sys.stderr)
        corpus = Corpus()
        corpus.coverage[source] = Coverage(source, notes=[note])
        return corpus


def workflow_summary(corpus) -> str:
    """Runs as the harness declared them beside the agents the recorder actually logged:
    a run whose agents are missing is a recorder gap, not a smaller run."""
    runs = corpus.workflow_runs
    logged = defaultdict(int)
    for i in corpus.invocations:
        if i.workflow_run:
            logged[i.workflow_run] += 1
    declared = sum(r.agents_declared or 0 for r in runs)
    status = Counter(r.status or "?" for r in runs)
    short = sum(1 for r in runs if (r.agents_declared or 0) > logged.get(r.run_id, 0))
    if runs:
        s = (f"{len(runs):,} workflow runs ({', '.join(f'{n} {k}' for k, n in status.most_common())}); "
             f"{declared:,} agents declared, {sum(logged[r.run_id] for r in runs):,} logged")
    else:
        s = "no workflow run records"
    if short:
        s += f"; {short} run(s) logged fewer agents than they declared"
    over = [r for r in runs if logged.get(r.run_id, 0) > (r.agents_declared or 0)]
    if over:
        why = (", all killed: a killed run's record stops counting at the kill"
               if all(r.status == "killed" for r in over) else "")
        s += f"; {len(over)} run(s) logged more agents than their run record declares{why}"
    orphans = len(set(logged) - {r.run_id for r in runs})
    if orphans:
        s += f"; {orphans} run id(s) on agents with no run record"
    return s


def message_summary(corpus) -> str:
    ms = corpus.messages
    first = min(m.at for m in ms if m.at)
    return (f"{len(ms):,} cross-session messages since {first:%Y-%m-%d}: "
            f"{sum(m.matched for m in ms):,} recorded at both ends, "
            f"{sum(1 for m in ms if m.delivery == 'new_turn'):,} woke an idle session, "
            f"{sum(m.notify_when_idle for m in ms):,} asked for an idle notice")


def load(sources: list[str], db: str | None = None, days: int = 120,
         routes: str | None = None, jsonl: list[str] | None = None,
         regression: str | None = None) -> Corpus:
    corpus = Corpus()
    if any(s in sources for s in ("claude", "codex", "cursor", "antigravity", "openrouter")):
        from . import pg
        dsn = pg.resolve_dsn(db)
    if "claude" in sources:
        from . import claude
        corpus.extend(guarded("claude", lambda: claude.load(dsn, days)))
    if "codex" in sources:
        from . import codex
        corpus.extend(guarded("codex", lambda: codex.load(dsn, days)))
    if "cursor" in sources:
        from . import cursor
        corpus.extend(guarded("cursor", lambda: cursor.load(dsn, days)))
    if "antigravity" in sources:
        from . import antigravity
        corpus.extend(guarded("antigravity", lambda: antigravity.load(dsn, days)))
    if "openrouter" in sources:
        from . import openrouter
        corpus.extend(guarded("openrouter", lambda: openrouter.load(dsn, days, routes)))
        if routes and corpus.route_policy is None:      # skipped source: the allowlist still applies
            corpus.route_policy = openrouter.load_policy(routes)
    elif routes:
        from . import openrouter
        corpus.route_policy = openrouter.load_policy(routes)
    if "local" in sources:
        from . import local
        corpus.extend(local.load(days, routes))
    for path in jsonl or []:
        from . import jsonl as jl
        corpus.extend(jl.load(path))
    corpus = corpus.finalize()
    if corpus.workflow_runs or any(i.workflow_run for i in corpus.invocations):
        corpus.coverage["workflows"] = Coverage("workflows", summary=workflow_summary(corpus))
    if corpus.messages:
        corpus.coverage["messages"] = Coverage("messages", summary=message_summary(corpus))
    from . import regression as rg
    path = regression or rg.discover(routes)
    if path:
        corpus.regression = rg.load(path)
        corpus.coverage["regression"] = Coverage(
            "regression", summary=f"latest run {corpus.regression['ran_at']:%Y-%m-%d}, "
                                  f"{len(corpus.regression['modes'])} mode(s), from {path}")
    return corpus
