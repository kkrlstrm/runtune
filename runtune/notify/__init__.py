"""Send one digest to every configured channel; collect the first valid answer from any.

A digest is one state file (.runtune/digests/<id>.json) with one delivery per
channel. `runtune inbox` polls every delivery; `runtune reply` answers locally. The
first reply that parses is acted on, confirmed where it came from, and the other
channels are told it was handled — so approving in Slack and then again locally
cannot apply anything twice.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from . import card, channels, digest


def send(ws, d: dict, only: list[str] | None = None, png: bool = True) -> dict:
    did = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_png = os.path.join(ws.root, "digests", f"{did}.png")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    img = card.render_png(card.build(d), out_png) if png else None
    text = digest.to_text(d)
    entries = channels.load_config(ws.root)
    if only:
        entries = [e for e in entries if e["type"] in only] or [{"type": t} for t in only]
    state = {"digest_id": did, "status": "awaiting_reply" if d["items"] else "informational",
             "items": d["items"], "sent_at": datetime.now(timezone.utc).isoformat(), "png": img,
             "deliveries": []}
    for e in entries:
        rec = {"type": e["type"], "entry": e}
        try:
            rec.update(channels.build(e, ws.root).send(d, text, img) or {})
            rec["ok"] = True
        except Exception as exc:  # noqa: BLE001 - one broken channel must not stop the others
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        state["deliveries"].append(rec)
    if not any(r["ok"] for r in state["deliveries"]):
        state["status"] = "undelivered"
    digest.save(ws, state)
    return state


def poll(ws, state: dict) -> list[tuple[dict, str]]:
    """(delivery, reply_text) pairs, oldest first within each channel."""
    out = []
    for rec in state.get("deliveries", []):
        if not rec.get("ok"):
            continue
        try:
            for text in channels.build(rec["entry"], ws.root).replies(rec):
                out.append((rec, text))
        except Exception as exc:  # noqa: BLE001
            rec["last_poll_error"] = f"{type(exc).__name__}: {exc}"
    return out


def answer(ws, state: dict, rec: dict, text: str, others: str | None = None) -> None:
    for r in state.get("deliveries", []):
        if not r.get("ok"):
            continue
        msg = text if r is rec else others
        if not msg:
            continue
        try:
            channels.build(r["entry"], ws.root).answer(r, msg)
        except Exception:  # noqa: BLE001
            pass


def handle(ws, state: dict, rec: dict, text: str, approver: str, target_root: str) -> dict:
    res = digest.act(ws, state, text, approver, target_root)
    other = f"Handled from {rec['type']}: {res['message']}" if res["status"] == "done" else None
    answer(ws, state, rec, res["message"], others=other)
    if res["status"] == "done":
        state["status"] = "executed"
        state["result"] = {k: res[k] for k in ("applied", "refused", "declined")}
        state["answered_via"] = rec["type"]
    return res
