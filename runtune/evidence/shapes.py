"""Clustering keys: what makes two attempts "the same thing, tried again".

Every stage in RunTune groups attempts before it counts them, and every count is
only as good as the key it was grouped by. This module holds the keys, in one
place, so the failure side (constraints) and the success side (capabilities)
cannot drift apart. Ported from callusguard's derive stage and extended with the
inline-program key the capability side needs.

    command_shape   `git push`, not `git`, not the full command line. The
                    denominator key: successes and failures share it.
    error_signature digits -> #, quoted strings -> 'S', whitespace squeezed. The
                    numerator key: only failures have one.
    inline_target   for an inline program (`python3 -c`, a heredoc, `node -e`),
                    what it imports from the local codebase. Two snippets that
                    both exist to import the same helper are the same capability
                    being re-derived, however different their text is.
"""

from __future__ import annotations

import os
import re

# Commands where the second token carries the meaning.
MULTIPLEXERS = {
    "git", "npm", "pnpm", "yarn", "npx", "docker", "kubectl", "cargo", "go", "pip",
    "pip3", "python", "python3", "psql", "aws", "gcloud", "make", "brew", "apt",
    "apt-get", "systemctl", "launchctl", "gh", "glab", "fly", "flyctl", "uv", "node",
    "bash", "sh", "zsh",
}

# Shell builtins and wrappers that say nothing about intent on their own. A rule
# keyed on `cd` or `for` is a real cluster and a worthless rule — callusguard's
# README lists exactly these as what naive derivation proposed.
LOW_SIGNAL_HEADS = {"cd", "set", "for", "while", "if", "export", "source", ".",
                    "echo", "true", "false", "sleep", "then", "do", "done", "{", "(", "&&", "||", ";",
                    "#", "\\", "declare", "local", "eval", "exec", "time", "nohup", "env"}

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def normalize_error(err: str | None, limit: int = 200) -> str:
    s = (err or "").lower()
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "'S'", s)
    s = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "U", s)   # URLs (with their query strings) vary per call
    s = re.sub(r"/[\w./-]+", "/P", s)            # paths vary per run
    s = re.sub(r"[0-9a-f]{8,}", "H", s)          # hashes and ids
    s = re.sub(r"[0-9]+", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


_PREFIXES = [
    re.compile(r"^#[^\n]*\n\s*"),                                    # leading comment lines
    re.compile(r"^cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;|\n)\s*"),  # cd <dir> &&
    re.compile(r"^(?:&&|\|\||;)\s*"),                                  # dangling operators
    re.compile(r"^[({]\s*"),                                           # subshell / group
    re.compile(r"^(?:set\s+-[a-z]+|export\s+\S+)\s*(?:&&|;|\n)\s*"),  # set -e; export X=..;
]


def _strip_prefix(command: str) -> str:
    """Drop what precedes the command's real head: comments, `cd x &&`, `set -e;`."""
    c = (command or "").strip()
    for _ in range(8):
        for rx in _PREFIXES:
            m = rx.match(c)
            if m:
                c = c[m.end():]
                break
        else:
            break
    return c


def first_tokens(command: str | None) -> list[str]:
    toks = [t for t in re.split(r"\s+", _strip_prefix(command or "")) if t]
    i = 0
    while i < len(toks) and _ENV_ASSIGN.match(toks[i]):
        if "$(" in toks[i]:              # X=$(cmd ...): the command substitution is the head
            inner = toks[i].split("$(", 1)[1]
            toks = ([inner] if inner else []) + toks[i + 1:]
            i = 0
            break
        i += 1
    if i >= len(toks):
        return []
    head = os.path.basename(toks[i])
    if head in MULTIPLEXERS and i + 1 < len(toks):
        nxt = toks[i + 1]
        if not nxt.startswith("-") and not nxt.startswith("<<"):
            # `python3 scripts/foo.py` keys on the script, not the directory
            return [head, os.path.basename(nxt) if "/" in nxt else nxt]
        if nxt in ("-c", "-e", "-"):
            return [head, nxt]
        if nxt.startswith("<<"):
            return [head, "<<"]
    return [head]


def command_shape(surface: str, command: str | None) -> str:
    """The denominator key. For shell surfaces, the head of the command; for
    everything else, the tool surface itself (MCP servers collapse to the server)."""
    if surface in ("Bash", "exec", "exec_command", "shell", "local_shell", "run_command"):
        toks = first_tokens(command)
        return " ".join(toks) if toks else ""
    if surface.startswith("mcp__"):
        parts = surface.split("__")
        if len(parts) >= 3 and parts[1]:
            return f"mcp__{parts[1]}__*"
    return surface


# Inline interpreters: their failures are tracebacks from arbitrary programs, so a rule
# on the shape would be a rule on "Python". They are the capability side's input.
INLINE_SHAPES = {"python3 -c", "python3 -", "python3 <<", "python -c", "python -", "python <<", "node -e",
                 "node <<", "bash -c", "sh -c", "zsh -c", "bash <<"}

# Read-only exploration: recurs everywhere, names no procedure.
EXPLORATORY_HEADS = {"ls", "grep", "rg", "find", "sed", "cat", "head", "tail", "wc", "awk", "sort",
                     "uniq", "cut", "tr", "xargs", "jq", "pwd", "which", "file", "stat", "du", "tree",
                     "diff", "less", "open", "date", "env", "printf", "test", "["}


def is_low_signal(shape: str) -> bool:
    head = (shape or "").split(" ")[0]
    return head in LOW_SIGNAL_HEADS or not head or shape in INLINE_SHAPES


def is_exploratory(shape: str) -> bool:
    return is_low_signal(shape) or (shape or "").split(" ")[0] in EXPLORATORY_HEADS


# --------------------------------------------------------------------------- #
# Inline programs — the capability side's key
# --------------------------------------------------------------------------- #
_INLINE = re.compile(
    r"(?:\bpython3?\s+(?:-c\b|-\s*<<)|\bnode\s+-e\b|<<\s*['\"]?(?:EOF|PY|PYEOF|END)\b)")
_PY_IMPORT = re.compile(
    r"(?:^|[\s;\"'(])(?:from\s+([A-Za-z_][\w.]*)\s+import\s+([\w, ]+)|import\s+([A-Za-z_][\w.]*))")

# Standard library and common third-party modules: importing these says nothing
# about which local capability a snippet is re-deriving.
_COMMON_MODULES = {
    "sys", "os", "json", "re", "csv", "time", "datetime", "pathlib", "collections",
    "subprocess", "math", "random", "itertools", "functools", "typing", "io",
    "hashlib", "base64", "glob", "shutil", "textwrap", "urllib", "statistics",
    "requests", "psycopg", "psycopg2", "pandas", "numpy", "yaml", "argparse",
    "dataclasses", "decimal", "string", "tempfile", "uuid", "zoneinfo", "http",
    "email", "html", "xml", "sqlite3", "asyncio", "concurrent", "logging", "pprint",
    "dotenv", "httpx", "bs4", "openpyxl", "unicodedata", "difflib", "operator", "copy",
    "traceback", "inspect", "ast", "pickle", "gzip", "zipfile", "struct", "socket",
    "ssl", "select", "signal", "threading", "multiprocessing", "queue", "heapq",
    "bisect", "fractions", "numbers", "enum", "abc", "contextlib", "warnings",
    "__future__", "platform", "getpass", "locale", "calendar", "secrets", "hmac",
}


_STDLIB = set(getattr(__import__("sys"), "stdlib_module_names", ())) | _COMMON_MODULES
_local_cache: dict[str, bool] = {}


def is_local_module(root: str) -> bool:
    """True when `root` is the codebase's own code, not the standard library or an
    installed package. Decided by asking this interpreter whether it can import the
    name WITHOUT the sys.path edits the snippets make: `toolkit` (reached through
    sys.path.insert) is local, `pypdf` (in site-packages) is not. So the answer is
    about the machine RunTune runs on — run it where the agents run, or list the
    roots in $RUNTUNE_LOCAL_ROOTS (comma-separated) to state it."""
    if root in _local_cache:
        return _local_cache[root]
    if root in os.environ.get("RUNTUNE_LOCAL_ROOTS", "").split(","):
        _local_cache[root] = True
        return True
    if root in _STDLIB or not root.isidentifier() or root[:1].isupper():
        _local_cache[root] = False
        return False
    import importlib.util
    try:
        found = importlib.util.find_spec(root) is not None
    except (ImportError, ValueError):
        found = False
    _local_cache[root] = not found
    return not found


def is_inline_program(command: str | None) -> bool:
    return bool(command) and bool(_INLINE.search(command))


def inline_target(command: str | None) -> str | None:
    """What local code an inline program exists to reach, or None.

    `from toolkit import db, crm` -> "toolkit:crm,db". The submodule list is kept
    because `toolkit:db` and `toolkit:gitlab` are different jobs that would get different
    CLIs. A snippet that imports only the standard library returns None: it is
    ad-hoc scripting, not a missing capability, and folding it in would make the
    largest cluster "people write Python".
    """
    if not is_inline_program(command):
        return None
    targets: dict[str, set[str]] = {}
    for m in _PY_IMPORT.finditer(command or ""):
        mod = m.group(1) or m.group(3)
        if not mod:
            continue
        root = mod.split(".")[0]
        if root in _COMMON_MODULES or not is_local_module(root):
            continue
        names = set()
        if m.group(2):
            names = {n.strip() for n in m.group(2).split(",") if n.strip()}
        if "." in mod:
            names.add(mod.split(".", 1)[1])
        targets.setdefault(root, set()).update(names)
    if not targets:
        return None
    root = sorted(targets, key=lambda r: -len(targets[r]))[0]
    subs = sorted(n for n in targets[root] if n.isidentifier())[:6]
    return f"{root}:{','.join(subs)}" if subs else root


# --------------------------------------------------------------------------- #
# Exit-status attribution — which part of a compound command a failure belongs to.
#
# A shell call reports ONE exit status for the whole line. In `a | head; echo ===; b`
# that status is b's, so keying the failure on `a` (the head) blames a command that
# succeeded: the "error" recorded for it is just its normal output. These helpers find
# the part whose status the call reports, or say that it cannot be told.

_CONTROL_WORDS = {"for", "while", "until", "if", "case", "select", "function", "do", "done",
                  "then", "else", "elif", "fi", "esac", "{", "}", "(", ")"}
# Segments that set up a command rather than do work. A leading `cd x &&` is not
# where a failure of the line comes from in practice.
_SETUP_HEADS = {"cd", "pushd", "export", "set", "unset", "source", ".", "umask", "ulimit", "true"}
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def split_top_level(command: str) -> tuple[list[tuple[str, str | None]], bool]:
    """Split a command line on its top-level operators. -> ([(segment, operator_after)], clean)

    Operators: `;` newline `&&` `||` `|` `&`. Quotes, `$( )`, backticks, comments,
    line continuations and heredoc bodies are never split. `clean` is False when the
    text ends inside an open quote or substitution (a truncated recording)."""
    s = command or ""
    segs, buf, i, n = [], [], 0, len(s)
    stack: list[str] = []          # "'", '"', "`", "$("
    depth: list[int] = []          # paren depth inside each "$(" frame
    pending: list[tuple[str, bool]] = []   # heredoc delimiters waiting for the next newline

    def cut(op):
        segs.append(("".join(buf), op))
        buf.clear()

    while i < n:
        ch, top = s[i], (stack[-1] if stack else None)
        if top == "'":
            buf.append(ch)
            if ch == "'":
                stack.pop()
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            if s[i + 1] == "\n" and top is None:      # line continuation
                i += 2
                continue
            buf.append(s[i:i + 2])
            i += 2
            continue
        if top == '"':
            if ch == '"':
                stack.pop()
            elif s.startswith("$(", i):
                stack.append("$(")
                depth.append(0)
                buf.append("$(")
                i += 2
                continue
            elif ch == "`":
                stack.append("`")
            buf.append(ch)
            i += 1
            continue
        if top == "`":
            buf.append(ch)
            if ch == "`":
                stack.pop()
            i += 1
            continue
        # top is None or "$(": quotes and substitutions open; only top level splits
        if ch in ("'", '"', "`"):
            stack.append(ch)
            buf.append(ch)
            i += 1
            continue
        if s.startswith("$(", i):
            stack.append("$(")
            depth.append(0)
            buf.append("$(")
            i += 2
            continue
        if top == "$(":
            if ch == "(":
                depth[-1] += 1
            elif ch == ")":
                if depth[-1] == 0:
                    stack.pop()
                    depth.pop()
                else:
                    depth[-1] -= 1
            buf.append(ch)
            i += 1
            continue
        # ---- top level
        if ch == "#" and (not buf or buf[-1][-1:].isspace()):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            m = _HEREDOC.match(s, i)
            if m:
                pending.append((m.group(3), m.group(1) == "-"))
                buf.append(m.group(0))
                i = m.end()
                continue
        if ch == "\n":
            if pending:                                   # skip heredoc bodies
                j = i + 1
                for delim, dash in pending:
                    while j < n:
                        k = s.find("\n", j)
                        line = s[j:] if k < 0 else s[j:k]
                        j = n if k < 0 else k + 1
                        if (line.lstrip("\t") if dash else line).strip() == delim:
                            break
                buf.append(s[i:j])
                pending.clear()
                i = j
                if i >= n:
                    break
                cut("\n")
                continue
            cut("\n")
            i += 1
            continue
        two = s[i:i + 2]
        if two in ("&&", "||"):
            cut(two)
            i += 2
            continue
        if two == "|&":
            cut("|")
            i += 2
            continue
        if ch == "|":
            cut("|")
            i += 1
            continue
        if ch == ";":
            cut(";")
            i += 1
            continue
        if ch == "&":
            prev = buf[-1][-1:] if buf else ""
            if prev in (">", "<") or s[i + 1:i + 2] == ">":    # 2>&1, >&2, &> — a redirect
                buf.append(ch)
                i += 1
                continue
            cut("&")
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        cut(None)
    clean = not stack
    return [(t.strip(), op) for t, op in segs if t.strip()], clean


def status_command(command: str | None, truncated: bool = False) -> str | None:
    """The part of `command` whose exit status the call reports, or None when it cannot be told.

    `;`/newline lists: the last one. A pipeline: its last element (no pipefail). An
    `&&`/`||` chain: unknown — any link may have failed — unless every link before the
    last only sets up (`cd x &&`). Loops, conditionals, groups and a backgrounded tail
    are unknown. A truncated recording that shows more than one part is unknown, since
    the part that decided the status may be cut off."""
    segs, _ = split_top_level(command or "")
    if not segs or segs[-1][1] == "&":
        return None
    if len(segs) == 1:
        return segs[0][0]
    if truncated:
        return None
    for text, _ in segs:
        toks = text.split()
        if toks and (toks[0] in _CONTROL_WORDS or toks[0].startswith(("(", "{"))):
            return None
    start = 0
    for k in range(1, len(segs)):
        if segs[k - 1][1] in (";", "\n", "&"):
            start = k
    last = segs[start:]
    pipelines, cur = [], []
    for text, op in last:
        cur.append(text)
        if op in ("&&", "||"):
            pipelines.append(cur)
            cur = []
    pipelines.append(cur)
    while len(pipelines) > 1 and len(pipelines[0]) == 1 and _is_setup(pipelines[0][0]):
        pipelines.pop(0)
    if len(pipelines) > 1:
        return None
    return pipelines[0][-1]


def _is_setup(text: str) -> bool:
    toks = text.split()
    if not toks:
        return True
    if all(_ENV_ASSIGN.match(t) for t in toks):
        return True
    return toks[0] in _SETUP_HEADS


def status_shape(surface: str, command: str | None, truncated: bool = False) -> str | None:
    """`command_shape` of the part the exit status belongs to; None when that cannot be told."""
    part = status_command(command, truncated)
    return None if part is None else command_shape(surface, part)
