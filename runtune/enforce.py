"""Enforcement at the tool boundary: the constraint side acting at run time.

Four graded actions, most-restrictive-wins:

    monitor   recorded in the audit log, never shown to the agent
    nudge     the rule's message is injected as context; the call still runs
    deny      the call is refused and the agent is told why
    block     exit 2 — the call is refused even under bypassPermissions

Two properties are not negotiable, because this code runs inside every tool call:

  * IT FAILS OPEN. A malformed ruleset, a bad regex, an unreadable file: the call
    proceeds and the fault is audited. The only thing allowed to stop an agent is a
    decision, never a crash.
  * THE CEILING HOLDS AT RUN TIME TOO. A rule whose action exceeds the ceiling its
    evidence earned (meta.action_ceiling) is enforced AT the ceiling, and the
    downgrade is audited. `apply` already refuses this; enforcing it again here
    means a hand-edited ruleset cannot arm a coin-flip as a block either.

Standard library only; importable under `python -S`.
"""

from __future__ import annotations

import json
import re

from .evidence.tiers import ACTION_RANK

FIELDS = {"command": ("command", "cmd"), "file_path": ("file_path", "path"), "url": ("url",),
          "content": ("content", "new_string"), "prompt": ("prompt",), "any": ()}


def load_rules(paths) -> tuple[list, list]:
    rules, faults = [], []
    for p in paths:
        try:
            with open(p) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - fail open, report
            faults.append(f"{p}: {exc}")
            continue
        rs = data.get("rules", []) if isinstance(data, dict) else data
        for r in rs if isinstance(rs, list) else []:
            if isinstance(r, dict) and r.get("id"):
                rules.append(r)
    return rules, faults


def _field_text(rule: dict, tool_input: dict) -> str:
    field = rule.get("field", "command")
    if field == "any":
        return json.dumps(tool_input, sort_keys=True)
    for k in FIELDS.get(field, (field,)):
        v = tool_input.get(k)
        if isinstance(v, list):
            v = " ".join(map(str, v))
        if isinstance(v, str):
            return v
    return ""


def _tool_matches(rule_tool: str | None, tool_name: str) -> bool:
    if not rule_tool:
        return True
    if rule_tool.endswith("*"):
        return tool_name.startswith(rule_tool[:-1])
    aliases = {"Bash": {"Bash", "exec", "exec_command", "shell", "local_shell", "Shell", "run_command"}}
    return tool_name in aliases.get(rule_tool, {rule_tool})


def fires(rule: dict, tool_name: str, tool_input: dict) -> bool:
    if not _tool_matches(rule.get("tool"), tool_name):
        return False
    text = _field_text(rule, tool_input)
    pats = rule.get("any") or ([rule["pattern"]] if rule.get("pattern") else [])
    if not pats:
        return False
    if not any(re.search(p, text) for p in pats):
        return False
    return not any(re.search(p, text) for p in rule.get("unless", []))


def effective_action(rule: dict) -> tuple[str, bool]:
    """(action, downgraded). Unknown actions are treated as monitor."""
    act = rule.get("action", "monitor")
    if act not in ACTION_RANK:
        return "monitor", True
    cap = (rule.get("meta") or {}).get("action_ceiling")
    if cap in ACTION_RANK and ACTION_RANK[act] > ACTION_RANK[cap]:
        return cap, True
    return act, False


def evaluate(rules: list, tool_name: str, tool_input: dict) -> dict:
    """Returns {action, fired:[{id, action, downgraded}], messages:[...], faults:[...]}."""
    fired, faults = [], []
    for r in rules:
        try:
            if fires(r, tool_name, tool_input):
                act, down = effective_action(r)
                fired.append({"id": r["id"], "action": act, "downgraded": down,
                              "message": r.get("message", "")})
        except re.error as exc:
            faults.append(f"{r.get('id')}: bad pattern ({exc})")
    action = max((f["action"] for f in fired), key=ACTION_RANK.get, default=None)
    shown = [f["message"] for f in fired if f["action"] != "monitor" and f["message"]]
    return {"action": action, "fired": fired, "messages": shown, "faults": faults}


def emit(verdict: dict, brand: str = "runtune") -> tuple[str, str, int]:
    """(stdout, stderr, exit_code) for Claude Code and Codex PreToolUse hooks."""
    act = verdict.get("action")
    msg = " ".join(verdict.get("messages") or []) or "blocked by a RunTune constraint"
    if act == "block":
        return "", f"BLOCKED by {brand}: {msg}\n", 2
    if act == "deny":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": "deny",
                                                  "permissionDecisionReason": msg}}), "", 0
    if act == "nudge":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "additionalContext": f"{brand}: {msg}"}}), "", 0
    return "", "", 0


def emit_cursor(verdict: dict, brand: str = "runtune") -> tuple[str, str, int]:
    """(stdout, stderr, exit_code) for Cursor's preToolUse. Cursor reads exit 2 as a
    deny with stderr as the reason, and otherwise a JSON {permission, user_message,
    agent_message, additional_context}. A nudge leaves `permission` unset: "allow"
    would also skip whatever approval Cursor itself would have asked for."""
    act = verdict.get("action")
    msg = " ".join(verdict.get("messages") or []) or "blocked by a RunTune constraint"
    if act == "block":
        return "", f"BLOCKED by {brand}: {msg}\n", 2
    if act == "deny":
        return json.dumps({"permission": "deny", "user_message": f"{brand}: {msg}",
                           "agent_message": msg}), "", 0
    if act == "nudge":
        return json.dumps({"additional_context": f"{brand}: {msg}"}), "", 0
    return "", "", 0
