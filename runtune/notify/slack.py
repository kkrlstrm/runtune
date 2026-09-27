"""Slack, standard library only: DM text, upload a PNG card, read replies in a thread.

Token: $RUNTUNE_SLACK_TOKEN (a bot token with chat:write, im:write, im:history,
files:write). Recipient: $RUNTUNE_SLACK_USER (a `U…` id). `files.upload` is sunset,
so uploads use the getUploadURLExternal -> POST -> completeUploadExternal flow.

$RUNTUNE_DRY_RUN=1 makes every write raise before a request is made — the safe
way to test a digest end to end.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

API = "https://slack.com/api/"


class SlackError(RuntimeError):
    pass


def _token() -> str:
    t = os.environ.get("RUNTUNE_SLACK_TOKEN")
    if not t:
        raise SlackError("RUNTUNE_SLACK_TOKEN is not set")
    return t


def _dry():
    if os.environ.get("RUNTUNE_DRY_RUN"):
        raise SlackError("RUNTUNE_DRY_RUN is set; nothing was sent")


def call(method: str, payload: dict | None = None, form: bool = False, get: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {_token()}"}
    url = API + method
    data = None
    if get:
        url += "?" + urllib.parse.urlencode(payload or {})
    elif form:
        data = urllib.parse.urlencode(payload or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload or {}).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if get else "POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        raise SlackError(f"{method}: {body.get('error')}")
    return body


def open_dm(user: str) -> str:
    return call("conversations.open", {"users": user})["channel"]["id"]


def post(channel: str, text: str, blocks: list | None = None, thread_ts: str | None = None) -> dict:
    _dry()
    p = {"channel": channel, "text": text, "unfurl_links": False}
    if blocks:
        p["blocks"] = blocks
    if thread_ts:
        p["thread_ts"] = thread_ts
    return call("chat.postMessage", p)


def upload_png(channel: str, path: str, title: str, comment: str = "", thread_ts: str | None = None) -> dict:
    _dry()
    size = os.path.getsize(path)
    up = call("files.getUploadURLExternal", {"filename": os.path.basename(path), "length": size}, form=True)
    with open(path, "rb") as f:
        req = urllib.request.Request(up["upload_url"], data=f.read(), method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req, timeout=60).read()
    p = {"files": [{"id": up["file_id"], "title": title}], "channel_id": channel}
    if comment:
        p["initial_comment"] = comment
    if thread_ts:
        p["thread_ts"] = thread_ts
    return call("files.completeUploadExternal", p)


def replies(channel: str, ts: str) -> list[dict]:
    """Messages in a thread, oldest first, the parent included."""
    return call("conversations.replies", {"channel": channel, "ts": ts, "limit": 200}, get=True).get("messages", [])


def history_after(channel: str, oldest: str) -> list[dict]:
    """Top-level messages after `oldest` — people often answer a DM without threading."""
    msgs = call("conversations.history", {"channel": channel, "oldest": oldest, "limit": 100}, get=True)
    return list(reversed(msgs.get("messages", [])))


def find_message_ts(channel: str, file_id: str) -> str | None:
    """The ts of the message a completed upload created (Slack does not return it)."""
    for m in call("conversations.history", {"channel": channel, "limit": 20}, get=True).get("messages", []):
        if any(f.get("id") == file_id for f in m.get("files", []) or []):
            return m["ts"]
    return None
