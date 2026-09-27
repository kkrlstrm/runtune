"""Channels: where a digest goes and where replies come back from.

    stdout   print the digest (and write the card PNG) — no credentials, no reply loop
    slack    DM to $RUNTUNE_SLACK_USER with the card image; replies read from that DM
    email    Gmail API thread (or SMTP, send-only); replies read from the thread

Only the approver's own replies are read: in Slack, messages whose `user` is
$RUNTUNE_SLACK_USER; in email, messages in the digest thread other than ours.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from . import card, digest


def send(ws, d: dict, channel: str, png: bool = True) -> dict:
    did = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_png = os.path.join(ws.root, "digests", f"{did}.png")
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    img = card.render_png(card.build(d), out_png) if png else None
    text = digest.to_text(d)
    state = {"digest_id": did, "channel": channel, "status": "awaiting_reply", "items": d["items"],
             "sent_at": datetime.now(timezone.utc).isoformat(), "sent_epoch": f"{time.time():.6f}", "png": img}
    if not d["items"]:
        state["status"] = "informational"
    if channel == "stdout":
        print(text)
        if img:
            print(f"\ncard: {img}")
        state["status"] = "printed"
    elif channel == "slack":
        from . import slack
        ch = slack.open_dm(os.environ["RUNTUNE_SLACK_USER"])
        if img:
            up = slack.upload_png(ch, img, "RunTune weekly", comment=text)
            ts = None
            for _ in range(10):   # the upload's message appears once Slack finishes processing
                ts = slack.find_message_ts(ch, up["files"][0]["id"])
                if ts:
                    break
                time.sleep(1.5)
        else:
            ts = slack.post(ch, text)["ts"]
        state.update(slack_channel=ch, slack_ts=ts)
    elif channel == "email":
        from . import email as em
        sent = em.send(f"RunTune — {d['chip'][0].lower()} ({len(d['items'])} proposals)", text.replace("*", ""),
                       html=card.build(d), png=img)
        state.update(email=sent)
    else:
        raise ValueError(f"unknown channel {channel}")
    digest.save(ws, state)
    return state


def replies(state: dict) -> list[str]:
    """New replies from the approver, oldest first."""
    if state["channel"] == "slack":
        from . import slack
        me = os.environ.get("RUNTUNE_SLACK_USER")
        seen = state.get("seen_ts", [])
        # Never read earlier than the digest itself: with no lower bound, an old message in
        # the same DM would be parsed as an approval of proposals it never saw.
        floor = state.get("slack_ts") or state.get("sent_epoch")
        if not floor:
            return []
        msgs = slack.replies(state["slack_channel"], state["slack_ts"]) if state.get("slack_ts") else []
        msgs += slack.history_after(state["slack_channel"], floor)
        out, keep = [], set(seen)
        for m in msgs:
            if m.get("ts") == state.get("slack_ts") or m.get("ts") in keep or m.get("user") != me:
                continue
            keep.add(m["ts"])
            out.append(m.get("text", ""))
        state["seen_ts"] = sorted(keep)
        return out
    if state["channel"] == "email":
        from . import email as em
        got = em.thread_replies(state["email"]["thread_id"], state["email"]["id"])
        new = got[state.get("seen_n", 0):]
        state["seen_n"] = len(got)
        return new
    return []


def answer(state: dict, text: str) -> None:
    if state["channel"] == "slack":
        from . import slack
        slack.post(state["slack_channel"], text, thread_ts=state.get("slack_ts"))
    elif state["channel"] == "email":
        from . import email as em
        em.send("Re: RunTune digest", text, thread_id=state["email"]["thread_id"])
