"""OpenRouter: the bill, and the calls your code makes.

  snapshot_activity()   GET /api/v1/activity (needs a provisioning key,
                        $OPENROUTER_PROVISIONING_KEY) -> ~/.runtune/spend/<day>.jsonl.
                        OpenRouter keeps only 30 days: a missed day is gone, so
                        schedule this daily.
  log_call(...)         call from your own router after every request. It records the
                        MODE the call claimed — the join key between the allowlist, the
                        call and the bill. A call site that bypasses this is exactly
                        what the route deriver's bill reconciliation finds.
"""

from __future__ import annotations

import inspect
import json
import os
import urllib.request
from datetime import datetime, timezone

from .. import home


def snapshot_activity() -> dict:
    key = os.environ.get("OPENROUTER_PROVISIONING_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_PROVISIONING_KEY is not set")
    req = urllib.request.Request("https://openrouter.ai/api/v1/activity",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.loads(r.read().decode()).get("data", [])
    by_day: dict[str, list] = {}
    for x in rows:
        d = str(x.get("date", ""))[:10]
        by_day.setdefault(d, []).append({"type": "spend", "usage_date": d, "model": x.get("model"),
                                         "requests": x.get("requests"), "usage_usd": x.get("usage"),
                                         "provider_name": x.get("provider_name")})
    os.makedirs(home.path("spend"), exist_ok=True)
    for d, rs in by_day.items():
        # one file per day, rewritten whole: the endpoint returns complete days
        with open(home.path("spend", f"{d}.jsonl"), "w") as f:
            for r in rs:
                f.write(json.dumps(r) + "\n")
    return {"days": len(by_day), "rows": len(rows)}


def log_call(mode: str | None, model: str, ok: bool, usd: float = 0.0, duration_ms: int | None = None,
             tokens_in: int = 0, tokens_out: int = 0, caller: str | None = None) -> None:
    """Record one routed model call. Never raises."""
    try:
        if caller is None:
            fr = inspect.stack()[1]
            caller = f"{os.path.basename(fr.filename)}:{fr.function}"
        ev = {"type": "event", "source": "openrouter", "kind": "model",
              "session": os.environ.get("RUNTUNE_JOB") or caller.split(":")[0],
              "ts": datetime.now(timezone.utc).isoformat(), "surface": mode or "(no mode)",
              "ok": bool(ok), "actor": caller, "model": model, "usd": usd, "duration_ms": duration_ms,
              "tokens_in": tokens_in, "tokens_out": tokens_out}
        with open(home.day_file("events"), "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:  # noqa: BLE001
        pass
