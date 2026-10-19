"""SESSIONS ACTING ON THE SAME THING -> did they tell each other?

Several agent sessions often run at once against one repository and one set of
deployed services. Claude Code lets one session message another (`SendMessage`,
https://code.claude.com/docs/en/cross-session-messaging), and a recorder that reads
both transcripts can join the two ends of each message. That makes three things
measurable that were not before:

  1. HANDOFFS WITHOUT A MESSAGE. A second session acts on a shared target (deploys the
     same app, edits the same file) soon after another session did, and no message
     passed between the two in between. A deploy from an older commit undoes the
     other session's deploy, and nothing errors. Counted only from the day messaging
     first appears in the evidence: before that, silence says nothing.

  2. MESSAGE FORM. The receiving user sees one preview line, so the first line has to
     name something they can check (a commit, a path, an app, a branch). The rules the
     docs and most teams' instructions give are measured per message: a checkable first
     line, the sender saying who it is, and whether it needs a reply.

  3. WAITING BY POLLING. A session that lists its peers again and again instead of
     subscribing once with `notify_when_idle` spends turns to learn what one notice
     would have told it.

None of these has an artifact RunTune can apply: what changes is how sessions work
together, which is the team's call. Approving one records it as done by hand.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta

from ..evidence import tiers
from .candidate import Candidate

WINDOWS = {"deploy": timedelta(hours=24), "file": timedelta(hours=2)}
MIN_HANDOFFS = 5        # handoffs inside the messaging era before a finding is admitted
MIN_WEEKS = 2
MIN_ERA_DAYS = 14       # messaging seen for less than this is a baseline, not a pattern
MIN_MESSAGES = 20       # sent messages before message form is judged
FORM_FLOOR = 0.6        # a rule followed by fewer than 60% of messages is a finding
POLL_CALLS, POLL_SPAN = 3, timedelta(minutes=30)
MIN_POLL_SESSIONS = 3
LEAD = timedelta(hours=1)  # a message up to an hour before the first action still counts

# `fly deploy`, or a project's own wrapper script with a `deploy <app>` subcommand.
_DEPLOY = re.compile(r"\bfly(?:ctl)?\s+deploy\b|\S+\.(?:py|sh)\s+deploy\b")
_APP = [re.compile(r"(?:^|\s)(?:-a|--app)[\s=]+([A-Za-z0-9][\w-]+)"),
        re.compile(r"\S+\.(?:py|sh)\s+deploy\s+([A-Za-z0-9][\w-]+)"),
        re.compile(r"--config\s+\S*?deploy/([\w-]+)/fly[\w.-]*\.toml"),
        re.compile(r"\bcd\s+\S*?deploy/([\w-]+)\b")]
_SCRATCH = ("/tmp/", "/private/tmp/", "/var/folders/")

# A first line names something a reader can check.
_CHECKABLE = re.compile(
    r"\b[0-9a-f]{7,40}\b"                          # a commit
    r"|[\w.-]+/[\w./-]+"                           # a path or branch (feat/x, scripts/a.py)
    r"|\b[\w-]+\.(?:py|ts|js|md|json|toml|sh|ya?ml|sql)\b"
    r"|https?://|#\d+|\bv\d+(?:\.\d+)*\b|`[^`]+`", re.I)
_SAYS_WHO = re.compile(r"\b(?:session|branch|worktree|this is)\b", re.I)
_REPLY = re.compile(r"\?|\b(?:no (?:reply|response|action)(?: is)? (?:needed|required)|"
                    r"(?:reply|response|answer)(?: is)? (?:needed|requested|required)|"
                    r"let me know|please (?:confirm|reply|answer)|fyi|heads[- ]up)\b", re.I)


def deploy_target(text: str) -> str | None:
    if not text or not _DEPLOY.search(text):
        return None
    for rx in _APP:
        m = rx.search(text)
        if m and not m.group(1).startswith("-"):
            return m.group(1)
    return None


def actions(corpus) -> dict:
    """(kind, target) -> [(ts, session, source)] for successful actions on a shared target."""
    out = defaultdict(list)
    for e in corpus.events:
        if e.kind != "tool" or not e.ok:
            continue
        if e.surface in ("Edit", "Write", "MultiEdit") and e.text.startswith("/"):
            if not e.text.startswith(_SCRATCH):
                out[("file", e.text)].append((e.ts, e.session, e.source))
        elif e.surface in ("Bash", "shell", "exec_command", "run_command"):
            app = deploy_target(e.text)
            if app:
                out[("deploy", app)].append((e.ts, e.session, e.source))
    return out


def handoffs(corpus) -> list[dict]:
    """Consecutive actions on one target by two different sessions inside its window."""
    out = []
    for (kind, target), acts in actions(corpus).items():
        acts.sort()
        seen = set()
        for (t1, s1, src), (t2, s2, _) in zip(acts, acts[1:]):
            if s1 == s2 or t2 - t1 > WINDOWS[kind]:
                continue
            k = (frozenset((s1, s2)), t2.date())
            if k in seen:
                continue
            seen.add(k)
            out.append({"kind": kind, "target": target, "source": src, "first": (t1, s1), "second": (t2, s2)})
    return out


def messaging_start(corpus):
    """The first moment the evidence shows cross-session messaging in use, or None."""
    ts = [m.at for m in corpus.messages if m.at]
    ts += [e.ts for e in corpus.events if e.surface in ("SendMessage", "ListAgents")]
    return min(ts) if ts else None


def corpus_end(corpus):
    ts = [e.ts for e in corpus.events] + [m.at for m in corpus.messages if m.at]
    return max(ts)


def _messaged(h, msgs) -> bool:
    (t1, s1), (t2, s2) = h["first"], h["second"]
    pair = {s1, s2}
    for m in msgs:
        if not m.at or not (t1 - LEAD <= m.at <= t2):
            continue
        if m.sender in pair and (m.receiver in pair or m.receiver is None):
            return True
        if m.receiver in pair and m.sender is None:
            return True
    return False


def derive(corpus) -> tuple[list, list]:
    out, withheld = [], []
    start = messaging_start(corpus)
    hs = handoffs(corpus)

    for kind in ("deploy", "file"):
        mine = [h for h in hs if h["kind"] == kind]
        if not mine:
            continue
        era = [h for h in mine if start and h["second"][0] >= start]
        silent = [h for h in era if not _messaged(h, corpus.messages)]
        weeks = len({h["second"][0].isocalendar()[:2] for h in era})
        targets = Counter(h["target"] for h in silent)
        noun = "deploys of one app" if kind == "deploy" else "edits of one file"
        window = WINDOWS[kind]
        hours = int(window.total_seconds() // 3600)
        c = Candidate(
            kind="coordination", key=f"handoff|{kind}",
            title=(f"{len(silent)} of {len(era)} cross-session {noun} within {hours}h had no message "
                   "between the two sessions") if era else
                  f"{len(mine)} cross-session {noun} within {hours}h, none since messaging was in use",
            claim=(f"A second session {'deployed' if kind == 'deploy' else 'edited'} the same "
                   f"{'app' if kind == 'deploy' else 'file'} within {hours}h of another session and no "
                   "cross-session message passed between them. Most often: "
                   + (", ".join(f"`{t}` ({n})" for t, n in targets.most_common(5)) or "none")),
            tier=tiers.RECURRING if weeks >= MIN_WEEKS else tiers.LOCAL, sources=sorted({h["source"] for h in mine}),
            numbers={"handoffs": len(mine), "since_messaging": len(era), "with_message": len(era) - len(silent),
                     "without_message": len(silent), "weeks": weeks, "window_hours": hours,
                     "messaging_seen_from": start.date().isoformat() if start else None,
                     "examples": [{"target": h["target"], "first": h["first"][0].isoformat(timespec="minutes"),
                                   "second": h["second"][0].isoformat(timespec="minutes"),
                                   "sessions": [h["first"][1][:8], h["second"][1][:8]]}
                                  for h in sorted(silent, key=lambda h: h["second"][0])[-5:]]},
            ladder=[("constraint", False, "a hook sees one session; whether another session just acted on this "
                                          "target needs the shared record"),
                    ("guidance", True, "tell the sessions on a target before acting on it, and lead with "
                                       "what changed")],
            requires=["human-review"],
            caveats=["a handoff is two sessions acting on one target in a window, not proof they conflicted",
                     "a message is matched by session, so one sent to a third session that relayed it is missed"])
        c.gate("messaging-in-use", bool(start),
               f"first seen {start:%Y-%m-%d}" if start else "no cross-session message in the evidence: "
               "before messaging, silence between sessions says nothing")
        c.gate("volume", len(era) >= MIN_HANDOFFS, f"{len(era)} handoffs since messaging (need {MIN_HANDOFFS})")
        era_days = (corpus_end(corpus) - start).days if start else 0
        c.numbers["messaging_days"] = era_days
        c.gate("breadth", weeks >= MIN_WEEKS and era_days >= MIN_ERA_DAYS,
               f"{weeks} week(s) with handoffs, messaging seen for {era_days} day(s) "
               f"(need {MIN_WEEKS} weeks and {MIN_ERA_DAYS} days)")
        c.gate("mostly-silent", bool(era) and len(silent) / len(era) >= 0.5,
               f"{len(silent)} of {len(era)} without a message")
        (out if c.admitted else withheld).append(c)

    for c in (message_form(corpus), polling(corpus)):
        if c:
            (out if c.admitted else withheld).append(c)
    return out, withheld


def message_form(corpus) -> Candidate | None:
    """Per sent message: does the first line name something checkable, does the sender say
    who it is, does it say whether it needs a reply. A pure `notify_when_idle`
    subscription has no body and is not judged."""
    sent = [m for m in corpus.messages if m.sender and m.first_line]
    if not sent:
        return None
    rules = {
        "checkable first line": lambda m: bool(_CHECKABLE.search(m.first_line)),
        "says who is sending": lambda m: bool(_SAYS_WHO.search(m.body or m.first_line)
                                              or (m.sender_name and m.sender_name in (m.body or ""))),
        "says whether it needs a reply": lambda m: bool(_REPLY.search(m.body or m.first_line)),
    }
    rates, misses = {}, {}
    for name, ok in rules.items():
        hit = [m for m in sent if ok(m)]
        rates[name] = round(len(hit) / len(sent), 2)
        misses[name] = [m.first_line[:120] for m in sent if not ok(m)][-3:]
    weak = [r for r, v in rates.items() if v < FORM_FLOOR]
    c = Candidate(
        kind="coordination", key="message-form",
        title=(f"cross-session messages: {', '.join(weak)} in under {int(FORM_FLOOR * 100)}% of {len(sent)}"
               if weak else f"cross-session messages: {len(sent)} sent, each rule followed in "
                            f"{int(FORM_FLOOR * 100)}%+"),
        claim=("The receiving user sees only the first line, so it should name a commit, path, app or branch "
               "they can check; the sender should say which session and branch it is; and it should say "
               "whether it needs a reply. Rates: " + "; ".join(f"{k} {int(v * 100)}%" for k, v in rates.items())),
        tier=tiers.RECURRING, sources=sorted({m.source for m in sent}),
        numbers={"messages": len(sent), "rates": rates, "examples_missing": {k: misses[k] for k in weak},
                 "matched_both_ends": sum(m.matched for m in corpus.messages)},
        ladder=[("constraint", False, "a hook could refuse a SendMessage on its first line, but the rule is "
                                      "judgement on content; start with the instruction"),
                ("guidance", True, "the instruction the messages are measured against")],
        requires=["human-review"],
        caveats=["the checks are patterns on text, not a reading: a first line can match and still say little"])
    c.gate("volume", len(sent) >= MIN_MESSAGES, f"{len(sent)} sent messages (need {MIN_MESSAGES})")
    c.gate("below-floor", bool(weak), "every rule followed" if not weak else f"{len(weak)} rule(s) below the floor")
    return c


def polling(corpus) -> Candidate | None:
    """Sessions that called ListAgents POLL_CALLS+ times within POLL_SPAN and never
    subscribed with notify_when_idle."""
    by = defaultdict(list)
    for e in corpus.events:
        if e.surface == "ListAgents":
            by[e.session].append(e.ts)
    if not by:
        return None
    subscribed = {m.sender for m in corpus.messages if m.notify_when_idle}
    pollers = []
    for sess, ts in by.items():
        ts.sort()
        if sess in subscribed:
            continue
        if any(ts[i + POLL_CALLS - 1] - ts[i] <= POLL_SPAN for i in range(len(ts) - POLL_CALLS + 1)):
            pollers.append(sess)
    sessions_listing = len(by)
    c = Candidate(
        kind="coordination", key="polling",
        title=f"{len(pollers)} of {sessions_listing} sessions polled their peers instead of asking for a notice",
        claim=(f"These sessions listed their peers {POLL_CALLS}+ times within {int(POLL_SPAN.total_seconds() // 60)} "
               "minutes and never subscribed with notify_when_idle, which sends one notice when the other "
               "session goes idle."),
        tier=tiers.RECURRING, sources=sorted({e.source for e in corpus.events if e.surface == "ListAgents"}),
        numbers={"sessions_listing_peers": sessions_listing, "polling_sessions": len(pollers),
                 "subscribed_sessions": len(subscribed)},
        ladder=[("guidance", True, "wait with notify_when_idle, never poll")],
        requires=["human-review"])
    c.gate("volume", len(pollers) >= MIN_POLL_SESSIONS, f"{len(pollers)} polling sessions (need {MIN_POLL_SESSIONS})")
    return c
