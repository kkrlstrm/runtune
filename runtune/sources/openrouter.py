"""OpenRouter: the model-routing half of the evidence.

Three inputs, each answering a different question:

  calls      (table $RUNTUNE_OR_CALLS_TABLE, default openrouter_calls) one row per request from our own router,
             tagged with the MODE it claimed, the model that served it, whether it
             came back ok, what it cost, and which caller file sent it.
             -> "what ran, for which job, and did it work"

  activity   (table $RUNTUNE_OR_ACTIVITY_TABLE, default openrouter_activity) OpenRouter's own per-day, per-model
             billing snapshot. It sees every request on the key, including ones
             that never went through the router.
             -> "what was billed" — the check on the calls log's completeness

  policy     routes.json — the allowlist: mode -> approved model, verified date,
             provider pin.
             -> "what was supposed to run"

The route deriver lives on the differences between these three. An allowlist is
a statement of intent; only the calls and the bill say what happened.

`ok` here means the request returned without error. It says nothing about
whether the answer was right — a model can be 100% ok and worst of five on
quality. Anything RunTune derives from `ok` is a reliability claim, never a
quality claim, and route candidates always carry "eval required".
"""

from __future__ import annotations

import json
import os
import re

from ..evidence.schema import Corpus, Coverage, Event
from . import pg

_CALLS_SQL = """
SELECT call_id, ts, coalesce(mode, '(no mode)') AS mode, model, usd, tok_in, tok_out, ok,
       route, caller_file, caller_func, entrypoint, label, duration_ms, host
FROM {calls_table} WHERE ts >= now() - make_interval(days => %s)
"""

_ACTIVITY_SQL = """
SELECT usage_date, model, provider_name, requests, prompt_tokens, completion_tokens,
       reasoning_tokens, usage_usd
FROM {activity_table} WHERE usage_date >= (now() - make_interval(days => %s))::date
"""


def _table(env: str, default: str) -> str:
    name = os.environ.get(env, default)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?", name):
        raise SystemExit(f"runtune: ${env}={name!r} is not a plain [schema.]table name")
    return name


def load(dsn: str, days: int = 120, routes_path: str | None = None) -> Corpus:
    """Column contract: docs/WAREHOUSE.md."""
    calls = _CALLS_SQL.format(calls_table=_table("RUNTUNE_OR_CALLS_TABLE", "openrouter_calls"))
    activity = _ACTIVITY_SQL.format(activity_table=_table("RUNTUNE_OR_ACTIVITY_TABLE", "openrouter_activity"))
    corpus = from_rows(pg.query(dsn, calls, [days]), pg.query(dsn, activity, [days]))
    if routes_path:
        corpus.route_policy = load_policy(routes_path)
    return corpus


def load_policy(path: str) -> dict:
    """The allowlist, plus every way it fails its schema (`errors`). RunTune analyses
    what parses either way; the routes deriver reports the errors as a finding."""
    with open(os.path.expanduser(path)) as f:
        data = json.load(f)
    modes = data.get("modes", data)
    errors, validator = policy_errors(data)
    return {
        "modes": {k: v for k, v in modes.items() if isinstance(v, dict) and v.get("model")},
        "retired": data.get("retired", {}),
        "staleness_warn_days": int(data.get("staleness_warn_days", 120)),
        "path": path,
        "errors": errors,
        "validator": validator,
    }


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def policy_errors(data) -> tuple[list[str], str]:
    """(errors, which validator found them). model-eval-gate owns the routes.json schema;
    when it is installed its validator is used, so RunTune, the TypeScript CLI and the
    Python router reach one verdict on one file. Without it, only the fields RunTune
    itself reads are checked, and the report says so."""
    try:
        from meg.routes_schema import validate_routes
        return validate_routes(data), "model-eval-gate"
    except ImportError:
        pass
    if not isinstance(data, dict) or not isinstance(data.get("modes"), dict):
        return ["not a routes.json document: no `modes` object"], "runtune"
    errs = []
    for name, m in data["modes"].items():
        if not isinstance(m, dict) or not m.get("model"):
            errs.append(f"modes.{name}.model: required non-empty string")
        elif not _ISO_DATE.match(str(m.get("verified_date", ""))):
            errs.append(f"modes.{name}.verified_date: must be an ISO date (YYYY-MM-DD)")
    for name, r in (data.get("retired") or {}).items():
        rd = r.get("retired_date") if isinstance(r, dict) else None
        if rd is not None and not _ISO_DATE.match(str(rd)):
            errs.append(f"retired.{name}.retired_date: must be an ISO date (YYYY-MM-DD)")
    return errs, "runtune (fields it reads; install model-eval-gate for the full schema)"


def from_rows(call_rows, activity_rows=()) -> Corpus:
    corpus = Corpus()
    cov = Coverage("openrouter")
    for r in call_rows:
        caller = os.path.basename(r.get("caller_file") or "") or (r.get("entrypoint") or "?")
        if r.get("caller_func"):
            caller += f":{r['caller_func']}"
        corpus.events.append(Event(
            source="openrouter", session=r.get("entrypoint") or r.get("host") or "?",
            ts=r["ts"], kind="model", surface=r["mode"], shape=r["mode"], ok=bool(r.get("ok")),
            actor=caller, text=(r.get("label") or "")[:120],
            error="" if r.get("ok") else "request not ok",
            model=r.get("model"), tokens_in=int(r.get("tok_in") or 0),
            tokens_out=int(r.get("tok_out") or 0), usd=float(r.get("usd") or 0),
            duration_ms=r.get("duration_ms"), billing="api",
        ))
    corpus.spend = [dict(r) for r in activity_rows]
    cov.notes.append("`ok` is transport success, not answer quality")
    corpus.coverage["openrouter"] = cov
    return corpus.finalize()
