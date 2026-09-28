"""The one hook RunTune installs: record every settled tool call, enforce every rule.

    python3 -m runtune.hook --host claude   # Claude Code: PreToolUse, PostToolUse, PostToolUseFailure
    python3 -m runtune.hook --host codex    # Codex CLI: PreToolUse, PostToolUse
    python3 -m runtune.hook --host cursor   # Cursor (~/.cursor/hooks.json): preToolUse

PreToolUse   -> evaluate the active rulesets; audit every rule that fired, emit the verdict
PostToolUse  -> append one settled attempt to ~/.runtune/events/<day>.jsonl
PostToolUseFailure -> the same, marked failed, with the error text

The host is passed explicitly rather than guessed from the environment: `CLAUDECODE=1`
is exported into every shell Claude Code spawns, so a Codex hook run from such a shell
would guess wrong. The one exception is Cursor, identified from the payload itself: Cursor
also runs the Claude Code hooks in ~/.claude/settings.json (on by default), so a hook
installed with `--host claude` gets called by Cursor too. Every Cursor payload carries
`cursor_version`, and its event names start lowercase (`preToolUse`).

For Cursor the hook ENFORCES and does not record. Cursor's Claude-hook import maps only
PreToolUse and PostToolUse, not PostToolUseFailure, so a hook-recorded Cursor corpus
would contain the successes and miss the failures. `runtune record cursor` reads every
outcome from Cursor's own store instead.

Everything here fails open — a recorder or guard fault never stops the agent.
Recorded text is redacted before it touches disk.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from . import enforce, home
from .evidence.redact import redact
from .lifecycle import ledger

_TEXT_KEYS = ("command", "cmd", "file_path", "path", "url", "query", "pattern", "skill",
              "description", "subagent_type")


def _text(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input or "")
    for k in _TEXT_KEYS:
        v = tool_input.get(k)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str) and v:
            return v
    return ""


def _failed_response(resp) -> tuple[bool, str]:
    """Claude reports most failures through PostToolUseFailure; Codex folds the exit
    code into the response. Read both."""
    if isinstance(resp, dict):
        code = resp.get("exit_code", resp.get("exitCode"))
        if isinstance(code, int) and code != 0:
            return True, str(resp.get("stderr") or resp.get("output") or f"exit {code}")[:400]
        if resp.get("is_error") or resp.get("error"):
            return True, str(resp.get("error") or "error")[:400]
    return False, ""


def record(host: str, p: dict, failed: bool) -> None:
    ti = p.get("tool_input") or {}
    err = ""
    if failed:
        err = str(p.get("error") or "")[:400]
    else:
        failed, err = _failed_response(p.get("tool_response"))
    ev = {"type": "event", "source": host if host in ("claude", "codex", "cursor", "antigravity") else "claude",
          "session": p.get("session_id") or "?", "ts": datetime.now(timezone.utc).isoformat(),
          "surface": p.get("tool_name") or "?", "text": redact(_text(ti), 1500), "ok": not failed,
          "actor": p.get("agent_type") or "root", "invocation": p.get("agent_id") or None,
          "model": p.get("model") or os.environ.get("RUNTUNE_MODEL")}
    if failed:
        ev["error"] = redact(err, 400)
    with open(home.day_file("events"), "a") as f:
        f.write(json.dumps(ev) + "\n")


def is_cursor(host: str, p: dict) -> bool:
    return host == "cursor" or "cursor_version" in p


def guard(p: dict, brand: str = "runtune", cursor: bool = False) -> tuple[str, str, int]:
    rules, faults = enforce.load_rules(home.rule_files(p.get("cwd")))
    ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
    v = enforce.evaluate(rules, p.get("tool_name") or "", ti)
    if v["fired"] or faults or v["faults"]:
        os.makedirs(home.root(), exist_ok=True)
        ledger.append(home.path("audit.jsonl"), {
            "action": "verdict", "verdict": v["action"], "session": p.get("session_id"),
            "tool": p.get("tool_name"), "tool_use_id": p.get("tool_use_id"),
            "fired": [{k: f[k] for k in ("id", "action", "downgraded")} for f in v["fired"]],
            "command_preview": redact(_text(ti), 160), "faults": faults + v["faults"]})
    return enforce.emit_cursor(v, brand) if cursor else enforce.emit(v, brand)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    host = argv[argv.index("--host") + 1] if "--host" in argv else "claude"
    try:
        p = json.loads(sys.stdin.read() or "{}")
        event = p.get("hook_event_name", "")
        cursor = is_cursor(host, p)
        if cursor:
            event = event[:1].upper() + event[1:]      # preToolUse -> PreToolUse
        if event == "PreToolUse":
            out, err, code = guard(p, cursor=cursor)
            if out:
                sys.stdout.write(out)
            if err:
                sys.stderr.write(err)
            return code
        if event in ("PostToolUse", "PostToolUseFailure") and not cursor:
            record(host, p, failed=event == "PostToolUseFailure")
    except Exception as exc:  # noqa: BLE001 - fail open, always
        try:
            with open(home.path("hook-errors.log"), "a") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {type(exc).__name__}: {exc}\n")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
