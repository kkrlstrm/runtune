"""Secret scrubbing for anything that leaves the telemetry store.

Every sample command, error string and evidence excerpt RunTune writes to disk
passes through `redact()` first. autoharness's regex set misses Postgres DSNs and
GitLab tokens; those are the two credentials most likely to appear in a GTM
engineer's shell history, so they are covered here explicitly.

Regex redaction is a backstop, not a guarantee. Evidence files are for a human
reviewer on the same machine; do not publish them.
"""

from __future__ import annotations

import re

_RULES = [
    (re.compile(r"\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"]+"), r"\1://[REDACTED]"),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-.]{12,}"), "glpat-[REDACTED]"),
    (re.compile(r"\bsk-(?:or-|ant-|proj-)?[A-Za-z0-9_\-]{16,}"), "sk-[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh_[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "xox-[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"\b\d{2,4}\|[A-Za-z0-9]{24,}"), "[REDACTED-TOKEN]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|DSN|URL)[A-Z0-9_]*)=(['\"]?)[^\s'\"]{6,}\2"),
     r"\1=[REDACTED]"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|apikey|password|sig)=)[^&\s'\"]+"), r"\1[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[PHONE]"),
]


def redact(text: str | None, limit: int | None = None) -> str:
    s = text or ""
    for pat, repl in _RULES:
        s = pat.sub(repl, s)
    if limit is not None and len(s) > limit:
        s = s[:limit] + "…"
    return s
