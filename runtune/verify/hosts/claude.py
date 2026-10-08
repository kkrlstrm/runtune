"""Claude Code: `claude -p --output-format stream-json`.

Containment uses all three layers: the shims, the PreToolUse hook (Write/Edit run outside
the OS sandbox), and Claude Code's own sandbox set strict. In `bypassPermissions` mode the
sandbox ALLOWS hosts outside its allowlist unless `network.strictAllowlist` is set.
`--setting-sources project` keeps the user's own settings, hooks and plugins out;
`--strict-mcp-config` with no config keeps every MCP server out, and the init event proves it.
"""

from __future__ import annotations

import json
import os

from .. import sandbox, trace
from . import Host

TOOLS = frozenset({"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "NotebookEdit",
                   "NotebookRead", "WebFetch", "WebSearch", "Agent", "Task", "TodoWrite", "Skill",
                   "AskUserQuestion", "BashOutput", "KillShell", "Monitor", "TaskCreate", "TaskGet",
                   "TaskList", "TaskUpdate", "TaskStop", "ToolSearch", "ExitPlanMode", "EnterPlanMode"})


def settings(run: str, state: str, real: dict, root: str, live: dict | None) -> dict:
    deny = sandbox.deny_paths(root, live)
    read_rules = [f"Read({g})" for g in sandbox.SECRET_GLOBS]
    for p in deny:
        read_rules += [f"Read(/{p})", f"Read(/{p}/**)"]
    fs = {"allowWrite": [run], "denyRead": deny + sandbox.SECRET_GLOBS}
    allow_read = [os.path.join(root, p) for p in (live or {}).get("read", [])]
    if allow_read:
        fs["allowRead"] = allow_read
    return {
        "sandbox": {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                    "autoAllowBashIfSandboxed": True,
                    "network": {"strictAllowlist": True,
                                "allowedDomains": list((live or {}).get("network", []))},
                    "filesystem": fs},
        "permissions": {"deny": ["WebFetch", "WebSearch", *read_rules]},
        "hooks": {"PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                                  "hooks": [{"type": "command",
                                             "command": f'"{real["python3"]}" "{state}/hook.py" claude'}]}]},
    }


def parse(run_dir: str) -> trace.RunRecord:
    r = trace.RunRecord(run_dir)
    for ev in trace._jsonl(os.path.join(run_dir, "trace.jsonl")):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            r.init = ev
        elif t == "result":
            r.result = ev
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    r.uses.append({"id": c.get("id"), "name": c.get("name"), "input": c.get("input") or {}})
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        r.results[c.get("tool_use_id")] = trace._text(c.get("content"))
    r.started = bool(r.init)
    r.model = r.init.get("model")
    r.skills = list(r.init.get("skills") or []) if r.init else None
    r.mcp = [m.get("name") for m in r.init.get("mcp_servers") or []] if r.init else None
    if r.result:
        r.finished = not r.result.get("is_error") and r.result.get("subtype") == "success"
        r.end_reason = "" if r.finished else str(r.result.get("subtype"))
        r.turns = r.result.get("num_turns")
        r.cost_usd = float(r.result.get("total_cost_usd") or 0)
        r.final = r.result.get("result") or ""
    return trace.harness_logs(r)


class Claude(Host):
    name = "claude"
    exe = ("claude",)
    skill_dirs = (".claude/skills",)
    agent_dirs = (".claude/agents",)
    tools = TOOLS

    def configure(self, kit, tasks, real, real_root, live):
        path = os.path.join(kit["state"], "settings.json")
        with open(path, "w") as f:
            json.dump(settings(kit["run"], kit["state"], real, real_root, live), f, indent=2)
        kit["settings"] = path
        return {"env": {}, "limits": []}

    def command(self, prompt, kit, tasks):
        cmd = [self.binary() or "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--setting-sources", "project", "--settings", kit["settings"], "--strict-mcp-config",
               "--permission-mode", "bypassPermissions", "--no-session-persistence",
               "--max-turns", str(int(tasks.get("max_turns", 25)))]
        return cmd + (["--model", tasks["model"]] if tasks.get("model") else [])

    def parse(self, run_dir):
        return parse(run_dir)
