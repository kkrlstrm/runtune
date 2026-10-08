#!/usr/bin/env python3
"""Pre-tool hook for verify runs. Standard library only; copied into each run's state dir.
Called as `hook.py <host>`; the host decides only how a deny is spelled.

It closes the routes the shims and the OS sandbox cannot see:
  - File-writing tools (Write, Edit, Codex's apply_patch, Cursor's edit tools) may run
    outside the sandbox. Every path they write is resolved through its symlinks and must
    stay inside the run directory.
  - A shell command naming an interpreter or network tool by absolute path skips the PATH
    shims.
  - A shell command that rewrites PATH, or runs `env -i` / `command -p`, does the same.

Fail-CLOSED: an error here denies the call. A false deny costs one run; a false allow could
write into the real repository.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

STATE = os.environ.get("RUNTUNE_VERIFY_STATE") or os.path.dirname(os.path.abspath(__file__))
RUN = os.path.dirname(STATE)
HOST = sys.argv[1] if len(sys.argv) > 1 else "claude"

_BINS = r"(?:python(?:3(?:\.\d+)?)?|curl|wget|psql|mysql|gh|glab|ssh|scp|rsync|claude|aws|gcloud|kubectl|fly|flyctl)"
ABS_BIN = re.compile(rf"(?:^|[\s;&|(`'\"=])/[^\s;&|]*/{_BINS}(?=$|[\s;&|)`'\"])")
PATH_TAMPER = re.compile(r"(?:^|[\s;&|(`])(?:export\s+)?PATH\s*=|\benv\s+-i\b|\bcommand\s+-p\b|\bhash\s+-[rp]\b")

SHELL = {"Bash", "Shell", "shell", "local_shell", "exec", "exec_command", "run_command", "run_terminal_cmd"}
WRITE = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "edit_file", "write_file", "write_to_file",
         "search_replace", "StrReplace", "Delete", "delete_file", "replace_file_content",
         "multi_replace_file_content", "create_file", "EditNotebook"}
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def _log(**kw) -> None:
    kw["t"] = round(time.time(), 3)
    with open(os.path.join(STATE, "hook.jsonl"), "a") as f:
        f.write(json.dumps(kw) + "\n")


def _deny(reason: str) -> None:
    msg = f"runtune verify: {reason}"
    if HOST == "cursor":
        print(json.dumps({"permission": "deny", "user_message": msg, "agent_message": msg}))
    elif HOST == "antigravity":
        print(json.dumps({"decision": "deny", "reason": msg}))
    else:   # Claude Code and Codex share the PreToolUse output format
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": msg}}))
    sys.stderr.write(msg + "\n")
    sys.exit(0)


def inside_run(path: str, run: str = RUN, cwd: str | None = None) -> bool:
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    path = os.path.normpath(path)
    probe = path
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    real = os.path.join(os.path.realpath(probe), os.path.relpath(path, probe))
    return os.path.normpath(real).startswith(os.path.realpath(run) + os.sep)


def tool_input(ev: dict) -> dict:
    inp = ev.get("tool_input")
    if inp is None:
        inp = ev.get("input") or ev.get("arguments") or {}
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except ValueError:
            inp = {"command": inp}
    return inp if isinstance(inp, dict) else {}


def write_paths(tool: str, inp: dict) -> list:
    """Every path a file-writing call would write. A patch names its files inside its text."""
    text = json.dumps(inp)
    if tool == "apply_patch" or "*** Begin Patch" in text:
        body = inp.get("patch") or inp.get("input") or inp.get("command") or ""
        if isinstance(body, list):
            body = "\n".join(map(str, body))
        return [a or b for a, b in _PATCH_PATH.findall(str(body))]
    keys = ("file_path", "path", "target_file", "TargetFile", "notebook_path", "filePath")
    return [inp[k].strip().strip('"') for k in keys if isinstance(inp.get(k), str) and inp[k].strip()]


def shell_text(inp: dict) -> str:
    c = inp.get("command") or inp.get("cmd") or inp.get("CommandLine") or ""
    if isinstance(c, list):
        c = c[-1] if len(c) >= 3 and c[1] in ("-lc", "-c") else " ".join(map(str, c))
    return str(c)


def main() -> None:
    ev = json.load(sys.stdin)
    if isinstance(ev.get("toolCall"), dict):        # Antigravity: {toolCall: {name, args}, workspacePaths}
        ev = {"tool_name": ev["toolCall"].get("name"), "tool_input": ev["toolCall"].get("args") or {},
              "cwd": (ev.get("workspacePaths") or [None])[0]}
    if "command" in ev and "tool_name" not in ev:    # Cursor beforeShellExecution: {command, cwd}
        ev = {"tool_name": "Shell", "tool_input": {"command": ev["command"]}, "cwd": ev.get("cwd")}
    tool = ev.get("tool_name") or ev.get("tool") or ""
    inp = tool_input(ev)
    cwd = ev.get("cwd") or (ev.get("workspace_roots") or [None])[0]
    patch = tool not in SHELL and "*** Begin Patch" in json.dumps(inp)
    if tool in WRITE or patch:
        paths = write_paths(tool, inp)
        if not paths:
            _log(tool=tool, decision="deny", why="write with no path")
            _deny(f"{tool} names no path this hook can check")
        for p in paths:
            if not inside_run(p, cwd=cwd):
                _log(tool=tool, decision="deny", path=p)
                _deny(f"writes stay inside the verify run; {p} is outside it (it would change the real repository)")
        _log(tool=tool, decision="allow", path=paths[0])
    elif tool in SHELL:
        cmd = shell_text(inp)
        if "*** Begin Patch" in cmd:            # Codex can run apply_patch through its shell tool
            for p in write_paths("apply_patch", {"patch": cmd}):
                if not inside_run(p, cwd=cwd):
                    _log(tool=tool, decision="deny", path=p)
                    _deny(f"writes stay inside the verify run; {p} is outside it")
        if ABS_BIN.search(cmd):
            _log(tool=tool, decision="deny", why="absolute-path binary", cmd=cmd)
            _deny("call interpreters and CLIs by name (`python3`, not `/usr/bin/python3`)")
        if PATH_TAMPER.search(cmd):
            _log(tool=tool, decision="deny", why="PATH tamper", cmd=cmd)
            _deny("changing PATH or bypassing command lookup is not allowed in a verify run")
        _log(tool=tool, decision="allow", cmd=cmd)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # fail closed
        _deny(f"hook error ({type(e).__name__}: {e}); denied to be safe")
