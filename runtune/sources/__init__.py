"""Evidence sources. Each returns a `Corpus`; `load` merges any combination."""

from __future__ import annotations

import sys

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
    from . import regression as rg
    path = regression or rg.discover(routes)
    if path:
        corpus.regression = rg.load(path)
        corpus.coverage["regression"] = Coverage(
            "regression", summary=f"latest run {corpus.regression['ran_at']:%Y-%m-%d}, "
                                  f"{len(corpus.regression['modes'])} mode(s), from {path}")
    return corpus.finalize()
