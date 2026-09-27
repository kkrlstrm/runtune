"""RunTune's own recordings: ~/.runtune/events and ~/.runtune/spend.

This is the source that needs nothing else installed. The hook writes Claude Code
and Codex tool calls here; `runtune record codex` backfills Codex from its rollout
files; `runtune record openrouter` snapshots the provider bill and `runtune.record.
openrouter.log_call()` records a routed call from application code. Invocations
(one sub-agent's life) are rebuilt from the events that carry an `invocation` id.
"""

from __future__ import annotations

import glob
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .. import home
from ..evidence.schema import Corpus, Invocation
from . import jsonl


def load(days: int = 120, routes: str | None = None) -> Corpus:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    corpus = Corpus()
    for kind in ("events", "spend"):
        for p in sorted(glob.glob(os.path.join(home.root(), kind, "*.jsonl"))):
            if os.path.basename(p)[:10] >= cutoff:
                corpus.extend(jsonl.load(p))
    by_inv = defaultdict(list)
    for e in corpus.events:
        if e.invocation and e.actor != "root":
            by_inv[(e.source, e.invocation)].append(e)
    for (src, inv), evs in by_inv.items():
        corpus.invocations.append(Invocation(
            source=src, invocation=inv, session=evs[0].session, agent_type=evs[0].actor,
            started=min(e.ts for e in evs), ended=max(e.ts for e in evs), status="completed",
            tools=Counter(e.surface for e in evs), failures=sum(not e.ok for e in evs)))
    if routes:
        from .openrouter import load_policy
        corpus.route_policy = load_policy(routes)
    return corpus.finalize()
