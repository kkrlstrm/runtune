"""The write guard: every file and message RunTune writes outside its workspace is checked first.

Redaction (`evidence/redact.py`) scrubs what RunTune *stores*. The guard checks what it
*writes*: a skill, an agent file, a ruleset rule, a route, a pull-request body, the files a
`--git` publish commits. It refuses; it never redacts, because an artifact with a hole cut
in it is not the artifact a person reviewed. No refusal repeats the value it matched.

Taken from @parcha/pi-agent-learn's guard (Apache-2.0; ideas reimplemented, no code copied):
secret values in their encoded forms, secret shapes, invisible Unicode, and case data. Case
data is the host's list of values that must never appear in an artifact (client names, tenant
ids, people). A skill or rule that names one is about a case, not about the work in general,
and an artifact can travel further than the evidence it came from (a team, a public repo).

Configuration, in authority.json:

    "guard": {
      "case_values": [{"path": "config/clients.json", "keys": ["slug", "name"]},
                      {"path": "case-values.txt"}],
      "ignore": ["mission"],
      "min_len": 4,
      "secret_env": ["MY_ODD_NAME"],
      "case_kinds": ["capability", "subagent"],
      "case_identifiers": "allow"
    }

A `path` is resolved against the target root. A `.json` source yields every string found
under the named `keys` at any depth (and every key of an object named by `keys: ["*key"]`);
any other file yields one value per line. Secrets are the values of environment variables
whose names look like credentials (KEY, TOKEN, SECRET, PASSWORD, DSN, ...) plus `secret_env`.

The defaults are strict, for artifacts that travel (a team, a public repo): case data is
checked in every kind and everywhere in the text. A private harness can narrow it:
`case_kinds` lists the artifact kinds checked for case data (a guard rule about one client's
database is specific on purpose), and `case_identifiers: "allow"` skips a value that is part of
a path or identifier (`deploy/acme-worker/`, `ACME_DB_URL`). Secrets, shapes and invisible
Unicode are checked in every kind regardless, and a pull-request body is always checked for
case data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|DSN|CREDENTIAL|_URL|AUTH)")
MIN_SECRET = 8

SHAPES = [
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{12,}")),
    ("api key", re.compile(r"\bsk-(?:or-|ant-|proj-)?[A-Za-z0-9_\-]{16,}")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("url with a password", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@'\"]+:[^\s/@'\"]+@")),
]

# Zero-width, bidirectional, tag, variation-selector and private-use characters; controls but
# tab/newline/CR. Joiners that are text are allowed (see _joiner_is_text).
_INVISIBLE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufe00-\ufe0e"
    "\U000e0000-\U000e007f\U000f0000-\U000ffffd\ue000-\uf8ff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Refusal:
    reason: str      # secret_value | secret_shape | invisible_unicode | case_data | path_not_allowed
    where: str       # file path or "message"
    detail: str      # never the matched value

    def __str__(self) -> str:
        return f"{self.reason} in {self.where}: {self.detail}"


class GuardRefused(Exception):
    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("; ".join(str(r) for r in refusals))


# ------------------------------------------------------------------ inputs
def env_secrets(extra_names: list[str] | None = None) -> list[str]:
    names = set(extra_names or [])
    out = []
    for k, v in os.environ.items():
        if (k in names or SECRET_NAME.search(k)) and v and len(v.strip()) >= MIN_SECRET:
            out.append(v.strip())
    return out


def _strings_under(obj, keys: set[str], take_keys: bool, grab: bool = False):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if take_keys and isinstance(k, str):
                yield k
            yield from _strings_under(v, keys, False, grab or k in keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings_under(v, keys, take_keys, grab)
    elif isinstance(obj, str) and grab:
        yield obj


def load_case_values(cfg: dict, target_root: str) -> list[tuple[str, str]]:
    """[(value, source label)] from the host's configured sources. A missing source is an
    error: a guard that silently checks nothing reads exactly like one that found nothing."""
    out: list[tuple[str, str]] = []
    for src in cfg.get("case_values", []):
        p = src["path"] if os.path.isabs(src["path"]) else os.path.join(target_root, src["path"])
        if not os.path.exists(p):
            raise FileNotFoundError(f"guard case_values source {src['path']} does not exist")
        label = os.path.basename(p)
        if p.endswith(".json"):
            with open(p) as f:
                data = json.load(f)
            keys = set(src.get("keys", []))
            take_keys = "*key" in keys
            for s in _strings_under(data, keys - {"*key"}, take_keys):
                out.append((s, label))
        else:
            with open(p) as f:
                out += [(ln.strip(), label) for ln in f if ln.strip() and not ln.startswith("#")]
    return out


# ------------------------------------------------------------------ matching
def _norm(s: str) -> str:
    """Unicode compatibility forms folded, case folded, whitespace runs kept as one space."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()


def _encodings(secret: str) -> set[str]:
    """The secret as written, without whitespace, hex, percent-encoded, and base64/base64url at
    each of the three byte alignments it can take inside a longer encoded stream."""
    b = secret.encode()
    forms = {secret, re.sub(r"\s+", "", secret), b.hex(), b.hex().upper(),
             urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
    for k, skip in ((0, 0), (1, 2), (2, 3)):
        full_groups = ((k + len(b)) // 3) * 4   # characters fixed by the secret's bytes alone
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            forms.add(enc(b"\0" * k + b).decode()[skip:full_groups])
    return {f for f in forms if len(f) >= MIN_SECRET}


def _id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def check_text(where: str, text: str, *, secrets: list[str] = (), case_values: list[tuple[str, str]] = (),
               ignore: set[str] = frozenset(), min_len: int = 4,
               allow_identifiers: bool = False) -> list[Refusal]:
    refusals: list[Refusal] = []
    flat = re.sub(r"\s+", "", text)
    for s in secrets:
        if any(form in text or form in flat for form in _encodings(s)):
            refusals.append(Refusal("secret_value", where, f"a configured secret (id {_id(s)})"))
    for name, rx in SHAPES:
        if rx.search(text):
            refusals.append(Refusal("secret_shape", where, f"something shaped like a {name}"))
    for m in _INVISIBLE.finditer(text):
        ch = m.group()
        if ch in "\u200c\u200d" and _joiner_is_text(text, m.start()):
            continue
        refusals.append(Refusal("invisible_unicode", where, f"U+{ord(ch):04X} at offset {m.start()}"))
        break
    norm = _norm(text)
    seen = set()
    for value, label in case_values:
        tokens = _norm(value).split()
        key = " ".join(tokens)
        if len(key.replace(" ", "")) < min_len or key in ignore or key in seen:
            continue
        # The value's words in order, any whitespace between them (a line break included), and
        # not inside a longer word: "spur" does not refuse "spurious".
        # Bounded by anything but a letter or digit: an underscore separates (`ACME_DB_URL`).
        rx = r"(?<![^\W_])" + r"\s+".join(re.escape(t) for t in tokens) + r"(?![^\W_])"
        hits = [m for m in re.finditer(rx, norm)
                if not (allow_identifiers and _in_identifier(norm, m.start(), m.end()))]
        if hits:
            seen.add(key)
            refusals.append(Refusal("case_data", where, f"a value from {label} (id {_id(value)})"))
    return refusals


_IDENT = "/-_.$@:"


def _in_identifier(text: str, start: int, end: int) -> bool:
    """Is text[start:end] part of a path, env var, module or host name (touching / - _ . $ @ :)?"""
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return (before and before in _IDENT) or (after and after in _IDENT)


def _joiner_is_text(text: str, i: int) -> bool:
    """A joiner inside an emoji sequence, or a joiner/non-joiner between two non-Latin letters
    (Persian, the Indic scripts and others need them), is text, not a hidden character."""
    if not 0 < i < len(text) - 1:
        return False
    a, b = text[i - 1], text[i + 1]

    def emoji(c: str) -> bool:
        return unicodedata.category(c) == "So" or 0x1F000 <= ord(c) <= 0x1FAFF

    def script_letter(c: str) -> bool:
        return ord(c) > 0x024F and unicodedata.category(c)[0] in "LM"

    return (text[i] == "\u200d" and emoji(a) and emoji(b)) or (script_letter(a) and script_letter(b))


def check_path(path: str, allowed_roots: list[str]) -> list[Refusal]:
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if any(p.casefold().rstrip(". ") == ".git" for p in parts):
        return [Refusal("path_not_allowed", path, "a .git path")]
    real = os.path.realpath(path)
    if allowed_roots and not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + os.sep)
                                 for r in allowed_roots):
        return [Refusal("path_not_allowed", path, "outside the configured targets")]
    return []


# ------------------------------------------------------------------ the guard
class Guard:
    def __init__(self, cfg: dict | None = None, target_root: str = ".", secrets: list[str] | None = None):
        cfg = cfg or {}
        self.cfg = cfg
        self.secrets = secrets if secrets is not None else env_secrets(cfg.get("secret_env"))
        self.case_values = load_case_values(cfg, target_root)
        self.ignore = {" ".join(_norm(x).split()) for x in cfg.get("ignore", [])}
        self.min_len = int(cfg.get("min_len", 4))
        self.case_kinds = set(cfg["case_kinds"]) if cfg.get("case_kinds") is not None else None
        self.allow_identifiers = cfg.get("case_identifiers", "refuse") == "allow"

    def check(self, files: dict[str, str], allowed_roots: list[str] | None = None,
              kind: str | None = None, kind_of=None) -> list[Refusal]:
        """`kind` is the artifact kind of every file; `kind_of(path)` names it per file when one
        check covers several (a publish). The message (a PR body) is always checked for case data."""
        out: list[Refusal] = []
        for where, text in files.items():
            if allowed_roots is not None and where != "message":
                out += check_path(where, allowed_roots)
            k = kind if kind is not None else (kind_of(where) if kind_of else None)
            case = (where == "message" or self.case_kinds is None or k in self.case_kinds)
            out += check_text(where, text, secrets=self.secrets,
                              case_values=self.case_values if case else (),
                              ignore=self.ignore, min_len=self.min_len,
                              allow_identifiers=self.allow_identifiers and where != "message")
        return out

    def require(self, files: dict[str, str], allowed_roots: list[str] | None = None) -> None:
        refusals = self.check(files, allowed_roots)
        if refusals:
            raise GuardRefused(refusals)


def artifact_texts(cand: dict, target: str) -> dict[str, str]:
    """What applying this candidate would write, as {path: text}."""
    p = cand.get("proposal", {}) or {}
    out = {}
    for key, fn in (("skill_md", "SKILL.md"), ("agent_md", None)):
        if p.get(key):
            out[os.path.join(target, fn) if fn else target] = p[key]
    for key in ("ruleset_rule", "grant", "route"):
        if p.get(key):
            out[f"{target}#{key}"] = json.dumps(p[key], sort_keys=True, default=str)
    if p.get("script"):
        out[f"{target}#script"] = p["script"]
    # The title and claim are not written to the target (they go to the digest, which quotes
    # evidence by design); the PR body is checked when it is published.
    return out


__all__ = ["Guard", "GuardRefused", "Refusal", "artifact_texts", "check_text", "check_path",
           "env_secrets", "load_case_values"]
