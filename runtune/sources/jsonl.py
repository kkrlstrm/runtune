"""A portable, dependency-free evidence format — for tests, demos, and any
recorder that isn't cc-logger / codex-logger / cursor-logger / the OpenRouter router.

One JSON object per line:

    {"type": "event", "source": "claude", "session": "s1", "ts": "2026-09-01T10:00:00Z",
     "surface": "Bash", "text": "psql -c 'select 1'", "ok": false, "error": "connection refused",
     "actor": "root", "model": "claude-opus-5"}
    {"type": "invocation", "source": "claude", "invocation": "a1", "session": "s1",
     "agent_type": "workflow-subagent", "status": "completed",
     "tools": {"Bash": 12, "Read": 2}, "tokens_out": 3000, "tokens_reread": 900000,
     "token_verified": true}
    {"type": "policy", "modes": {"extract-bulk": {"model": "qwen/…", "verified_date": "2026-05-18"}}}

`shape`, `error` normalization and `inline_target` are derived here with the same
functions the Postgres sources use, so a fixture clusters exactly like production.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from ..evidence import shapes
from ..evidence.redact import redact
from ..evidence.schema import Corpus, Event, Invocation


def _ts(v) -> datetime | None:
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load(path: str) -> Corpus:
    corpus = Corpus()
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            r = json.loads(line)
            kind = r.get("type", "event")
            if kind == "policy":
                corpus.route_policy = {"modes": r.get("modes", {}), "retired": r.get("retired", {}),
                                       "staleness_warn_days": int(r.get("staleness_warn_days", 120)),
                                       "path": path}
            elif kind == "spend":
                corpus.spend.append(r)
            elif kind == "invocation":
                corpus.invocations.append(Invocation(
                    source=r.get("source", "jsonl"), invocation=r["invocation"], session=r.get("session", "?"),
                    agent_type=r.get("agent_type", "root"), started=_ts(r.get("started")),
                    ended=_ts(r.get("ended")), status=r.get("status", "completed"), model=r.get("model"),
                    tokens_out=int(r.get("tokens_out", 0)), tokens_reread=int(r.get("tokens_reread", 0)),
                    token_verified=bool(r.get("token_verified", False)),
                    tools=Counter(r.get("tools", {})), failures=int(r.get("failures", 0))))
            else:
                surface = r.get("surface", "Bash")
                text = r.get("text", "")
                ok = bool(r.get("ok", True))
                is_model = r.get("kind") == "model" or r.get("source") == "openrouter"
                corpus.events.append(Event(
                    source=r.get("source", "jsonl"), session=r.get("session", "?"), ts=_ts(r["ts"]),
                    kind="model" if is_model else "tool", surface=surface,
                    shape=r.get("shape") or (surface if is_model else shapes.command_shape(surface, text)),
                    ok=ok, actor=r.get("actor", "root"), invocation=r.get("invocation"),
                    text=redact(text, 800), chars=len(text), error="" if ok else shapes.normalize_error(r.get("error")),
                    model=r.get("model"), tokens_in=int(r.get("tokens_in", 0)),
                    tokens_out=int(r.get("tokens_out", 0)), usd=float(r.get("usd", 0)),
                    duration_ms=r.get("duration_ms"),
                    inline_target=None if is_model else shapes.inline_target(text)))
    return corpus.finalize()
