"""Cursor CLI: `agent -p --output-format stream-json`.

Built from Cursor's documented formats (cursor.com/docs/cli, /reference/sandbox, /hooks) and
not yet run on a machine with the CLI installed; `runtune verify --selftest --host cursor`
must pass before a verdict counts (see verify.run).

Each run gets its own HOME, so Cursor's user-level config is out: ~/.cursor (MCP servers,
hooks, sandbox policy), and the ~/.claude settings, skills and agents Cursor imports by
default (on this machine that would fire cc-logger's Claude Code hooks). The project's own
.claude/settings.json hooks still load, as they would in production. Auth is the CLI's own
login (`agent login`, kept in the macOS Keychain, which a run's HOME does not hide) or
CURSOR_API_KEY. A repository's .cursor/sandbox.json outranks the user-level
one and .cursor/mcp.json starts MCP servers, so both are removed from each run's copy.

Containment is Cursor's sandbox, from the run's ~/.cursor/sandbox.json:
  - writes: the workspace (the project copy) plus the run's state/ and tmp/
  - reads: `readBoundary: "workspace"`, plus the shims and harness files the run needs;
    the real repository, its .runtune and the home's credential directories are outside it
  - network: deny by default; in live mode, the task file's domains
plus permission deny rules for the agent's own Read tool, and the hook (fail-closed).
Cursor documents no environment filter, so CURSOR_API_KEY is visible to the agent's shell.
"""

from __future__ import annotations

import json
import os
import sys

from .. import sandbox, trace
from . import Host
from .claude import TOOLS as CLAUDE_TOOLS

# stream-json tool keys -> the shared vocabulary
_KIND = {"shellToolCall": "Bash", "readToolCall": "Read", "writeToolCall": "Write", "editToolCall": "Edit",
         "deleteToolCall": "Edit", "grepToolCall": "Grep", "globToolCall": "Glob", "lsToolCall": "LS",
         "webSearchToolCall": "WebSearch", "webFetchToolCall": "WebFetch", "taskToolCall": "Agent"}


def sandbox_json(run: str, live: dict | None) -> dict:
    return {"type": "workspace_readwrite",
            "additionalReadwritePaths": [os.path.join(run, "state"), os.path.join(run, "tmp")],
            "readBoundary": "workspace",
            "additionalReadPaths": [os.path.join(run, "bin"), os.path.join(run, "state"), sys.prefix,
                                    sys.base_prefix],
            "networkPolicy": {"default": "deny", "allow": list((live or {}).get("network", []))}}


def cli_config(root: str, live: dict | None) -> dict:
    deny = [f"Read({p}/**)" for p in sandbox.deny_paths(root, live)] + [f"Read({p})" for p in sandbox.deny_paths(root, live)]
    deny += [f"Read({g})" for g in sandbox.SECRET_GLOBS] + ["Read(**/.env*)", f"Write({root}/**)"]
    # Cursor's docs put the read boundary in sandbox.json in one place and in cli-config.json
    # (sandbox.readBoundary) in another; it is set in both
    return {"version": 1, "permissions": {"allow": ["Shell(*)", "Read(**)", "Write(**)"], "deny": deny},
            "sandbox": {"mode": "enabled", "readBoundary": "workspace",
                        "networkAccess": "allowlist" if (live or {}).get("network") else "none"}}


# project files that would widen the run: a repository's .cursor/sandbox.json takes priority over
# the user-level one, and .cursor/mcp.json starts MCP servers
PROJECT_OVERRIDES = (".cursor/sandbox.json", ".cursor/mcp.json")


def logged_in(exe: str) -> bool:
    """`agent status` exits 0 when the CLI has a login. Its default store is the macOS
    Keychain, which a run's own HOME does not hide."""
    import subprocess
    try:
        return subprocess.run([exe, "status"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def hooks_json(state: str, python: str) -> dict:
    cmd = f'"{python}" "{state}/hook.py" cursor'
    return {"version": 1, "hooks": {"preToolUse": [{"command": cmd, "failClosed": True}],
                                    "beforeShellExecution": [{"command": cmd, "failClosed": True}]}}


def _args_input(name: str, args: dict) -> dict:
    a = dict(args or {})
    if name == "Bash":
        a.setdefault("command", a.get("cmd") or a.get("command") or "")
    if name in ("Read", "Write", "Edit") and "path" in a:
        a.setdefault("file_path", a["path"])
    return a


def _result_text(res) -> str:
    if not isinstance(res, dict):
        return "" if res is None else str(res)
    ok = res.get("success") if isinstance(res.get("success"), dict) else res
    for k in ("stdout", "output", "content", "text"):
        if isinstance(ok.get(k), str):
            out = ok[k]
            if isinstance(ok.get("stderr"), str) and ok["stderr"]:
                out += "\n" + ok["stderr"]
            return out
    err = res.get("error") or res.get("failure")
    return json.dumps(err if err else res)[:4000]


def parse(run_dir: str, skill_dirs=()) -> trace.RunRecord:
    r = trace.RunRecord(run_dir)
    texts = []
    for ev in trace._jsonl(os.path.join(run_dir, "trace.jsonl")):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            r.started, r.init, r.model = True, ev, ev.get("model")
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"])
        elif t == "tool_call" and ev.get("subtype") == "completed":
            tc = ev.get("tool_call") or {}
            cid = ev.get("call_id")
            if "function" in tc:
                fn = tc["function"] or {}
                args = fn.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except ValueError:
                    args = {"raw": args}
                name, body = fn.get("name") or "?", {"args": args, "result": fn.get("result")}
            else:
                key = next(iter(tc), "")
                body = tc.get(key) or {}
                if key == "mcpToolCall":
                    a = body.get("args") or {}
                    name = f"mcp__{a.get('providerIdentifier') or a.get('server')}__{a.get('toolName') or a.get('name')}"
                else:
                    name = _KIND.get(key, key.replace("ToolCall", ""))
            r.uses.append({"id": cid, "name": name, "input": _args_input(name, body.get("args") or {})})
            r.results[cid] = _result_text(body.get("result"))
        elif t == "usage" and isinstance(ev.get("usage"), dict):
            for k, v in ev["usage"].items():
                if isinstance(v, (int, float)):
                    r.tokens[k] = r.tokens.get(k, 0) + int(v)
        elif t == "result":
            r.result = ev
            r.finished = not ev.get("is_error") and ev.get("subtype") == "success"
            r.end_reason = "" if r.finished else str(ev.get("subtype") or ev.get("error") or "error")
            r.final = ev.get("result") or ""
            u = ev.get("usage") or {}
            if u:     # the result's usage is the run's total
                r.tokens = {k: int(v or 0) for k, v in u.items() if isinstance(v, (int, float))}
    if not r.final and texts:
        r.final = texts[-1]
    r.mcp = sorted({u["name"].split("__")[1] for u in r.uses if u["name"].startswith("mcp__")})
    trace.skill_reads(r, skill_dirs)
    return trace.harness_logs(r)


class Cursor(Host):
    name = "cursor"
    exe = ("agent", "cursor-agent", "~/.local/bin/agent", "~/.local/bin/cursor-agent")
    skill_dirs = (".agents/skills", ".cursor/skills", ".claude/skills", ".codex/skills")
    agent_dirs = (".cursor/agents", ".claude/agents", ".codex/agents")
    tools = CLAUDE_TOOLS              # Cursor reads Claude-format agent files

    def ready(self):
        if not self.binary():
            return ("Cursor's CLI is not installed (`agent`; https://cursor.com/docs/cli/installation), "
                    "or set RUNTUNE_VERIFY_CURSOR_BIN")
        if not os.environ.get("CURSOR_API_KEY") and not logged_in(self.binary()):
            return ("Cursor's CLI is not signed in: run `agent login` (kept in the Keychain, which runs can use), "
                    "or set CURSOR_API_KEY")
        return None

    def configure(self, kit, tasks, real, real_root, live):
        run, state = kit["run"], kit["state"]
        home = os.path.join(run, "home")
        cdir = os.path.join(home, ".cursor")
        os.makedirs(cdir, exist_ok=True)
        os.makedirs(os.path.join(run, "tmp"), exist_ok=True)
        for name, doc in (("sandbox.json", sandbox_json(run, live)), ("cli-config.json", cli_config(real_root, live)),
                          ("hooks.json", hooks_json(state, real["python3"]))):
            with open(os.path.join(cdir, name), "w") as f:
                json.dump(doc, f, indent=2)
        removed = [rel for rel in PROJECT_OVERRIDES if os.path.lexists(os.path.join(kit["proj"], rel))]
        for rel in removed:
            os.unlink(os.path.join(kit["proj"], rel))
        env = {"HOME": home, "TMPDIR": os.path.join(run, "tmp"), "CURSOR_CONFIG_DIR": cdir}
        if os.environ.get("CURSOR_API_KEY"):
            env["CURSOR_API_KEY"] = os.environ["CURSOR_API_KEY"]
        return {"env": env, "limits": [
            "Cursor documents no environment filter: CURSOR_API_KEY is visible to the agent's shell",
            "Cursor reports neither loaded skills nor cost; skill use is counted from reads of SKILL.md",
            "the project's .claude/settings.json hooks run, as they do in production",
            "a login from `agent login` is in the Keychain, which the agent's shell may be able to query"]
            + [f"removed the project's {rel} from the run's copy: it would widen the sandbox or start MCP servers"
               for rel in removed]}

    def command(self, prompt, kit, tasks):
        cmd = [self.binary() or "agent", "-p", "--output-format", "stream-json", "--trust", "--force",
               "--sandbox", "enabled", "--workspace", kit["proj"]]
        if tasks.get("model"):
            cmd += ["--model", tasks["model"]]
        return cmd + [prompt]

    def parse(self, run_dir):
        return parse(run_dir, self.skill_dirs)
