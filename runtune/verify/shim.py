#!/usr/bin/env python3
"""Command shim for verify runs. Standard library only: copied into each run's state dir
and executed by the wrappers on the run's PATH (python3, curl, psql, ...) and by wrappers
overlaid on intercepted repo executables (`scripts/toolkit`).

For each call, in order:
  1. a mock matches the canonical command line -> answer from the mock, record it
  2. strict mode: a repo script, repo module, intercepted executable or network binary that
     no mock covers -> refuse (exit 97) and record `unmocked`; nothing runs
  3. otherwise run it for real (inline `python3 -c`, local git; everything in live mode),
     recording repo-level calls so measures can see them

Canonical line: a python script's basename plus args (`toolkit.py db master`), `-m mod args`
for modules, the repo-relative path for an intercepted executable (`scripts/toolkit db`),
and `<bin> args` for anything else. Mocks match it with re.search.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

PY_NAMES = {"python", "python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"}
PY_FLAG_WITH_ARG = {"-X", "-W", "-Q"}
GIT_NETWORK = {"push", "pull", "fetch", "clone", "ls-remote", "remote", "submodule"}
UNMOCKED = 97
STATE = os.environ.get("RUNTUNE_VERIFY_STATE") or os.path.dirname(os.path.abspath(__file__))


def _cfg() -> dict:
    with open(os.path.join(STATE, "mocks.json")) as f:
        return json.load(f)


def record(**kw) -> None:
    kw.setdefault("t", round(time.time(), 3))
    fd = os.open(os.path.join(STATE, "effects.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(kw) + "\n").encode())
    finally:
        os.close(fd)


def python_target(args: list) -> tuple:
    """-> (kind, target, rest). kind: script | module | inline."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "-"):
            return "inline", None, args
        if a == "-m":
            return ("module", args[i + 1], args[i + 2:]) if i + 1 < len(args) else ("inline", None, args)
        if a.startswith("-"):
            i += 2 if a in PY_FLAG_WITH_ARG else 1
            continue
        return "script", a, args[i + 1:]
    return "inline", None, args


def _under(path: str, roots: list) -> bool:
    rp = os.path.realpath(path)
    return any(rp.startswith(os.path.realpath(r) + os.sep) for r in roots if r)


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith("RUNTUNE_VERIFY_")}


def _refuse(canon: str, why: str) -> int:
    record(kind="unmocked", cmd=canon, cwd=os.getcwd(), why=why)
    sys.stderr.write(f"VERIFY-UNMOCKED: `{canon}` is not available in this verify run ({why}). Nothing ran.\n")
    return UNMOCKED


def _answer(mock: dict, canon: str) -> int:
    record(kind=mock.get("kind", "read"), mock=mock["id"], cmd=canon, cwd=os.getcwd())
    out = mock.get("stdout") or ""
    if mock.get("stdout_file"):
        with open(mock["stdout_file"]) as f:
            out = f.read()
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if mock.get("stderr"):
        sys.stderr.write(mock["stderr"] + "\n")
    return int(mock.get("exit", 0))


def main(argv: list) -> int:
    name, args = argv[1], argv[2:]
    cfg = _cfg()
    live = cfg.get("mode") == "live"
    roots = [cfg["repo"], cfg["project"]]
    real = None

    if name.startswith("@"):                        # an intercepted repo executable
        rel = name[1:]
        canon, kind, real = shlex.join([rel, *args]), "repo-exec", os.path.join(cfg["repo"], rel)
    elif name in PY_NAMES:
        kind, target, rest = python_target(args)
        real = cfg["real"].get(name) or cfg["real"]["python3"]
        if kind == "script":
            canon = shlex.join([os.path.basename(target), *rest])
            script = target if os.path.isabs(target) else os.path.join(os.getcwd(), target)
            if _under(script, roots):
                kind = "repo-script"
        elif kind == "module":
            canon = shlex.join(["-m", target, *rest])
            if any(target == m or target.startswith(m + ".") for m in cfg.get("repo_modules", [])):
                kind = "repo-module"
        else:
            canon = shlex.join([name, *args])
    else:
        kind, canon, real = "bin", shlex.join([name, *args]), cfg["real"].get(name)

    if kind != "inline":
        for mock in cfg.get("mocks", []):
            if re.search(mock["match"], canon):
                return _answer(mock, canon)

    if kind in ("repo-exec", "repo-script", "repo-module"):
        if not live and not any(re.search(p, canon) for p in cfg.get("passthrough", [])):
            return _refuse(canon, f"{kind} with no mock (strict mode)")
        record(kind="call", cmd=canon, cwd=os.getcwd(), live=live)
    elif kind == "bin":
        if name == "git":
            sub = next((a for a in args if not a.startswith("-")), "")
            if sub in GIT_NETWORK and not live:
                return _refuse(canon, f"git {sub} reaches a remote")
        elif not live:
            return _refuse(canon, f"`{name}` reaches an external system")
        else:
            record(kind="call", cmd=canon, cwd=os.getcwd(), live=True)
    if not real:
        return _refuse(canon, f"`{name}` is not installed")
    os.execve(real, [real, *args], _clean_env())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
