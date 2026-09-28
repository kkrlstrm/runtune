"""Evidence sources. Each returns a `Corpus`; `load` merges any combination."""

from __future__ import annotations

from ..evidence.schema import Corpus


def load(sources: list[str], db: str | None = None, days: int = 120,
         routes: str | None = None, jsonl: list[str] | None = None) -> Corpus:
    corpus = Corpus()
    if any(s in sources for s in ("claude", "codex", "cursor", "openrouter")):
        from . import pg
        dsn = pg.resolve_dsn(db)
    if "claude" in sources:
        from . import claude
        corpus.extend(claude.load(dsn, days))
    if "codex" in sources:
        from . import codex
        corpus.extend(codex.load(dsn, days))
    if "cursor" in sources:
        from . import cursor
        corpus.extend(cursor.load(dsn, days))
    if "openrouter" in sources:
        from . import openrouter
        corpus.extend(openrouter.load(dsn, days, routes))
    elif routes:
        from . import openrouter
        corpus.route_policy = openrouter.load_policy(routes)
    if "local" in sources:
        from . import local
        corpus.extend(local.load(days, routes))
    for path in jsonl or []:
        from . import jsonl as jl
        corpus.extend(jl.load(path))
    return corpus.finalize()
