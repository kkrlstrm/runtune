"""Email, standard library only. Two transports:

  gmail   the Gmail API with an OAuth user token file ($RUNTUNE_GMAIL_TOKEN, the
          authorized-user JSON that google-auth writes: token, refresh_token,
          client_id, client_secret, token_uri). The access token is refreshed here
          with one POST; no Google client library is needed. Replies are read from
          the same thread, which is what makes reply-to-approve work.
  smtp    $RUNTUNE_SMTP_HOST/PORT/USER/PASS — send only (no reply loop).

Recipient: $RUNTUNE_EMAIL_TO. $RUNTUNE_DRY_RUN=1 refuses every send.
"""

from __future__ import annotations

import base64
import json
import os
import smtplib
import urllib.parse
import urllib.request
from email.message import EmailMessage

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"


class EmailError(RuntimeError):
    pass


def _access_token() -> str:
    path = os.path.expanduser(os.environ.get("RUNTUNE_GMAIL_TOKEN", ""))
    if not path or not os.path.exists(path):
        raise EmailError("RUNTUNE_GMAIL_TOKEN does not point at a token file")
    with open(path) as f:
        tok = json.load(f)
    data = urllib.parse.urlencode({"client_id": tok["client_id"], "client_secret": tok["client_secret"],
                                   "refresh_token": tok["refresh_token"],
                                   "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(tok.get("token_uri", "https://oauth2.googleapis.com/token"), data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def _gmail(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(GMAIL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {_access_token()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def build(subject: str, text: str, html: str | None = None, png: str | None = None,
          to: str | None = None, in_reply_to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["To"] = to or os.environ.get("RUNTUNE_EMAIL_TO", "")
    m["Subject"] = subject
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    if png and os.path.exists(png):
        with open(png, "rb") as f:
            m.add_attachment(f.read(), maintype="image", subtype="png", filename=os.path.basename(png))
    return m


def send(subject: str, text: str, html: str | None = None, png: str | None = None,
         thread_id: str | None = None, in_reply_to: str | None = None) -> dict:
    if os.environ.get("RUNTUNE_DRY_RUN"):
        raise EmailError("RUNTUNE_DRY_RUN is set; nothing was sent")
    msg = build(subject, text, html, png, in_reply_to=in_reply_to)
    if os.environ.get("RUNTUNE_SMTP_HOST"):
        with smtplib.SMTP(os.environ["RUNTUNE_SMTP_HOST"], int(os.environ.get("RUNTUNE_SMTP_PORT", "587"))) as s:
            s.starttls()
            s.login(os.environ["RUNTUNE_SMTP_USER"], os.environ["RUNTUNE_SMTP_PASS"])
            s.send_message(msg)
        return {"transport": "smtp"}
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    sent = _gmail("POST", "messages/send", body)
    return {"transport": "gmail", "id": sent["id"], "thread_id": sent["threadId"]}


def thread_replies(thread_id: str, sent_id: str) -> list[str]:
    """Plain-text bodies of messages in the thread other than the one we sent,
    with quoted history cut off."""
    th = _gmail("GET", f"threads/{thread_id}?format=full")
    out = []
    for msg in th.get("messages", []):
        if msg["id"] == sent_id:
            continue
        text = _plain(msg.get("payload", {}))
        keep = []
        for line in text.splitlines():
            if line.startswith(">") or (line.startswith("On ") and line.rstrip().endswith("wrote:")):
                break
            keep.append(line)
        out.append("\n".join(keep).strip())
    return out


def _plain(part: dict) -> str:
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "ignore")
    for p in part.get("parts", []) or []:
        t = _plain(p)
        if t:
            return t
    return ""
