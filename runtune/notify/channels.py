"""Channels: where a digest goes, and where the answer comes back from.

The default is LOCAL and needs nothing: the digest and its card are written to
.runtune/inbox/, a desktop notification points at them, and you answer with
`runtune reply 1,3`. Everything else is an extension you switch on in
.runtune/channels.json (or with `runtune channels add …`):

    {"channels": [
      {"type": "local"},
      {"type": "slack", "user": "U0123", "token_env": "RUNTUNE_SLACK_TOKEN"},
      {"type": "email", "to": "me@example.com"},
      {"type": "plugin", "module": "mypkg.teams:TeamsChannel", "any": "options"}
    ]}

Credentials are never stored in the config — only the NAME of the environment
variable that holds them. A plugin is any class with the three methods below; it
receives its own config entry as keyword arguments.

    send(digest: dict, text: str, png: str | None) -> dict   # delivery state to keep
    replies(delivery: dict) -> list[str]                      # new replies, oldest first
    answer(delivery: dict, text: str) -> None                 # confirm in the same place
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import shutil
import subprocess
import time


class Local:
    """Files + a desktop notification. Replies arrive through `runtune reply`."""

    def __init__(self, root: str, desktop: bool = True, **_):
        self.root, self.desktop = root, desktop

    def _dir(self) -> str:
        d = os.path.join(self.root, "inbox")
        os.makedirs(d, exist_ok=True)
        return d

    def send(self, digest, text, png):
        d = self._dir()
        md = os.path.join(d, "latest.md")
        with open(md, "w") as f:
            f.write(text.replace("*", "**").replace("_", "*") + "\n")
        if png:
            shutil.copy(png, os.path.join(d, "latest.png"))
        n = len(digest["items"])
        if self.desktop and not os.environ.get("RUNTUNE_NO_DESKTOP"):
            notify_desktop("RunTune", f"{n} proposal{'s' if n != 1 else ''} to review — run `runtune show`")
        return {"path": md}

    def replies(self, delivery):
        pending = delivery.pop("queued_replies", [])
        return pending

    def answer(self, delivery, text):
        with open(os.path.join(self._dir(), "log.md"), "a") as f:
            f.write(f"\n{time.strftime('%Y-%m-%d %H:%M')} — {text}\n")
        print(text)


class Slack:
    def __init__(self, user: str, token_env: str = "RUNTUNE_SLACK_TOKEN", **_):
        self.user, self.token_env = user, token_env

    def _env(self):
        if self.token_env != "RUNTUNE_SLACK_TOKEN" and os.environ.get(self.token_env):
            os.environ["RUNTUNE_SLACK_TOKEN"] = os.environ[self.token_env]

    def send(self, digest, text, png):
        from . import slack
        self._env()
        ch = slack.open_dm(self.user)
        sent_epoch = f"{time.time():.6f}"
        ts = None
        if png:
            up = slack.upload_png(ch, png, "RunTune weekly", comment=text)
            for _ in range(10):   # the upload's message appears once Slack finishes processing
                ts = slack.find_message_ts(ch, up["files"][0]["id"])
                if ts:
                    break
                time.sleep(1.5)
        else:
            ts = slack.post(ch, text)["ts"]
        return {"channel": ch, "ts": ts, "sent_epoch": sent_epoch}

    def replies(self, delivery):
        from . import slack
        self._env()
        # Never read earlier than the digest itself: with no lower bound, an old message in
        # the same DM would be parsed as an approval of proposals it never saw.
        floor = delivery.get("ts") or delivery.get("sent_epoch")
        if not floor:
            return []
        msgs = slack.replies(delivery["channel"], delivery["ts"]) if delivery.get("ts") else []
        msgs += slack.history_after(delivery["channel"], floor)
        seen, out = set(delivery.get("seen_ts", [])), []
        for m in msgs:
            if m.get("ts") == delivery.get("ts") or m.get("ts") in seen or m.get("user") != self.user:
                continue
            seen.add(m["ts"])
            out.append(m.get("text", ""))
        delivery["seen_ts"] = sorted(seen)
        return out

    def answer(self, delivery, text):
        from . import slack
        self._env()
        slack.post(delivery["channel"], text, thread_ts=delivery.get("ts"))


class Email:
    def __init__(self, to: str | None = None, **_):
        self.to = to

    def send(self, digest, text, png):
        from . import card, email
        if self.to:
            os.environ.setdefault("RUNTUNE_EMAIL_TO", self.to)
        sent = email.send(f"RunTune — {digest['chip'][0].lower()} ({len(digest['items'])} proposals)",
                          text.replace("*", ""), html=card.build(digest), png=png)
        return sent

    def replies(self, delivery):
        from . import email
        if delivery.get("transport") != "gmail":
            return []            # SMTP is send-only
        got = email.thread_replies(delivery["thread_id"], delivery["id"])
        new = got[delivery.get("seen_n", 0):]
        delivery["seen_n"] = len(got)
        return new

    def answer(self, delivery, text):
        from . import email
        if delivery.get("transport") == "gmail":
            email.send("Re: RunTune digest", text, thread_id=delivery["thread_id"])


BUILTIN = {"local": Local, "slack": Slack, "email": Email}


def load_config(root: str) -> list[dict]:
    p = os.path.join(root, "channels.json")
    try:
        with open(p) as f:
            return json.load(f).get("channels", []) or [{"type": "local"}]
    except FileNotFoundError:
        return [{"type": "local"}]


def save_config(root: str, chans: list[dict]) -> None:
    with open(os.path.join(root, "channels.json"), "w") as f:
        json.dump({"_readme": "Where RunTune digests go. Credentials are env var NAMES, never values.",
                   "channels": chans}, f, indent=2)


def build(entry: dict, root: str):
    kind = entry.get("type")
    opts = {k: v for k, v in entry.items() if k not in ("type", "module")}
    if kind == "plugin":
        mod, _, cls = entry["module"].partition(":")
        return getattr(importlib.import_module(mod), cls)(**opts)
    if kind not in BUILTIN:
        raise ValueError(f"unknown channel type {kind!r} (local, slack, email, plugin)")
    if kind == "local":
        opts["root"] = root
    return BUILTIN[kind](**opts)


def notify_desktop(title: str, text: str) -> None:
    """Best effort; a missing notifier is never an error."""
    try:
        if platform.system() == "Darwin":
            script = f'display notification {json.dumps(text)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text], timeout=5, capture_output=True)
    except Exception:  # noqa: BLE001
        pass
