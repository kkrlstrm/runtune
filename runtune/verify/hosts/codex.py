"""Codex CLI: `codex exec --json`.

Every run gets its own CODEX_HOME and HOME. The user's ~/.codex holds MCP servers (with
their credentials in plaintext), plugins, trusted-project entries and hooks, and Codex
also loads ~/.agents/skills, so neither home may be the real one. Auth is CODEX_API_KEY
when the caller has one; otherwise the run's CODEX_HOME links to the real auth.json, so a
token refresh lands in the one real file.

Containment is Codex's own Seatbelt sandbox, through a permission profile:
  - writes: the project copy (the workspace), the run's state/ and tmp/; the real
    repository is pinned read-only even where it sits under a writable temp directory
  - reads denied: credential directories in the home, the real repository's .runtune,
    .git and .env files, both Codex homes
  - network: off; in live mode, Codex's proxy with the task file's domains only
  - the shell inherits the already-allowlisted environment minus CODEX_*/OPENAI_*, and
    `allow_login_shell = false` keeps /etc/profile's path_helper from moving system paths
    ahead of the shims
The hook is wired in the throwaway CODEX_HOME (`--dangerously-bypass-hook-trust`, since a
fresh home has no stored trust). The project is NOT trusted, so its .codex/config.toml,
.codex/rules and .codex/hooks.json do not load; AGENTS.md and .agents/skills load either way.
Codex's docs call hooks a guardrail, not a boundary; the sandbox is the boundary.

Codex reports no skill-load event and no cost. Skill loading is checked before the run with
`codex debug prompt-input` (no model call), and tokens are recorded instead of cost.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

from .. import sandbox, trace
from . import Host

APP_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


def _q(s: str) -> str:
    return json.dumps(s)          # a TOML basic string is a JSON string


def config_toml(run: str, root: str, live: dict | None, real_home: str) -> str:
    deny = sandbox.deny_paths(root, live) + [os.path.join(run, "codexhome"), os.path.join(real_home, ".codex")]
    net = bool(live and live.get("network"))
    lines = ['approval_policy = "never"', 'default_permissions = "verify"', "allow_login_shell = false", "",
             "[history]", 'persistence = "none"', "",
             "[features]", "apps = false", "plugins = false", "browser_use = false", "computer_use = false",
             "memories = false", "hooks = true"] + (["network_proxy = true"] if net else []) + [
             "",
             "[shell_environment_policy]", 'inherit = "all"', "ignore_default_excludes = false",
             'exclude = ["CODEX_*", "OPENAI_*"]', "",
             "[permissions.verify]", 'extends = ":workspace"', "",
             "[permissions.verify.filesystem]",
             f'{_q(root)} = "read"',
             f'{_q(os.path.join(run, "state"))} = "write"',
             f'{_q(os.path.join(run, "tmp"))} = "write"']
    lines += [f'{_q(p)} = "deny"' for p in sorted(set(deny))]
    if net:
        lines += ["", "[permissions.verify.network]", "enabled = true", 'mode = "full"', "",
                  "[permissions.verify.network.domains]"] + [f'{_q(d)} = "allow"' for d in live["network"]]
    return "\n".join(lines) + "\n"


def hooks_json(state: str, python: str) -> dict:
    cmd = f'"{python}" "{state}/hook.py" codex'
    return {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": cmd, "timeout": 15}]}]}}


def _unwrap(command) -> str:
    """`bash -lc 'ls -la'` -> `ls -la`: the measures read what the agent asked to run."""
    if isinstance(command, list):
        toks = [str(t) for t in command]
    else:
        try:
            toks = shlex.split(str(command))
        except ValueError:
            return str(command)
    if len(toks) >= 3 and os.path.basename(toks[0]) in ("bash", "zsh", "sh") and toks[1] in ("-lc", "-c"):
        return toks[2]
    return " ".join(toks) if isinstance(command, list) else str(command)


def parse(run_dir: str, skill_dirs=(".agents/skills",)) -> trace.RunRecord:
    r = trace.RunRecord(run_dir)
    mcp = set()
    for ev in trace._jsonl(os.path.join(run_dir, "trace.jsonl")):
        t = ev.get("type")
        if t == "thread.started":
            r.started, r.init = True, ev
        elif t == "turn.completed":
            r.finished, r.result = True, ev
            for k, v in (ev.get("usage") or {}).items():
                r.tokens[k] = r.tokens.get(k, 0) + int(v or 0)
            r.turns = (r.turns or 0) + 1
        elif t in ("turn.failed", "error"):
            r.finished = False
            r.end_reason = ((ev.get("error") or {}).get("message") if t == "turn.failed" else ev.get("message")) or t
        elif t == "item.completed":
            it = ev.get("item") or {}
            kind, iid = it.get("type") or it.get("item_type"), it.get("id")
            if kind == "command_execution":
                r.uses.append({"id": iid, "name": "Bash", "input": {"command": _unwrap(it.get("command"))},
                               "exit_code": it.get("exit_code")})
                r.results[iid] = it.get("aggregated_output") or ""
            elif kind == "file_change":
                for i, ch in enumerate(it.get("changes") or []):
                    r.uses.append({"id": f"{iid}:{i}", "name": "Write" if ch.get("kind") == "add" else "Edit",
                                   "input": {"file_path": ch.get("path"), "kind": ch.get("kind")}})
            elif kind == "mcp_tool_call":
                mcp.add(it.get("server"))
                r.uses.append({"id": iid, "name": f"mcp__{it.get('server')}__{it.get('tool')}",
                               "input": it.get("arguments") or {}})
            elif kind == "collab_tool_call":
                r.uses.append({"id": iid, "name": "Agent", "input": {"prompt": it.get("prompt"), "tool": it.get("tool")}})
            elif kind == "web_search":
                r.uses.append({"id": iid, "name": "WebSearch", "input": {"query": it.get("query")}})
            elif kind == "agent_message" and (it.get("text") or "").strip():
                r.final = it["text"]
    if r.started and not r.finished and not r.end_reason:
        r.end_reason = "no turn.completed event"
    r.mcp = sorted(m for m in mcp if m)
    trace.skill_reads(r, skill_dirs)
    pre = os.path.join(run_dir, "state", "skills.json")
    if os.path.exists(pre):
        with open(pre) as f:
            r.skills = json.load(f).get("skills")
    return trace.harness_logs(r)


def skills_in_prompt(text: str, skill_dirs=(".agents/skills",)) -> list:
    """Project skills Codex lists to the model. Codex names each skill's file through a root
    alias (`(file: r1/use-toolkit/SKILL.md)`, with "`r1` = `/…/proj/.agents/skills`" defined
    once), so a skill counts only when its root is a project skill directory; Codex's own
    .system skills are left out."""
    text = text.replace("\\n", "\n")
    roots = {a: p for a, p in re.findall(r"`(r\d+)` = `([^`]+)`", text)}
    project = {a for a, p in roots.items() if any(p.rstrip("/").endswith("/" + d) for d in skill_dirs)}
    found = {n for a, n in re.findall(r"\(file: (r\d+)/([\w.-]+)/SKILL\.md\)", text) if a in project}
    rx = re.compile(r"(?:" + "|".join(re.escape(d) for d in skill_dirs) + r")/([\w.-]+)/SKILL\.md")
    return sorted(found | set(rx.findall(text)))


class Codex(Host):
    name = "codex"
    exe = ("codex", APP_BIN, "~/.codex/plugins/.plugin-appserver/codex-cli/bin/codex")
    skill_dirs = (".agents/skills",)
    agent_dirs = ()               # Codex agents are TOML under .codex/agents; RunTune drafts markdown
    tools = frozenset()

    def ready(self):
        if not self.binary():
            return ("Codex's CLI was not found (`codex`, or the copy inside ChatGPT.app); "
                    "or set RUNTUNE_VERIFY_CODEX_BIN")
        if not (os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")
                or os.path.exists(os.path.expanduser("~/.codex/auth.json"))):
            return "Codex is not signed in: run `codex login`, or set CODEX_API_KEY"
        return None

    def _env(self, run: str) -> dict:
        env = {"CODEX_HOME": os.path.join(run, "codexhome"), "HOME": os.path.join(run, "home"),
               "TMPDIR": os.path.join(run, "tmp")}
        key = os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if key:
            env["CODEX_API_KEY"] = key
        return env

    def configure(self, kit, tasks, real, real_root, live):
        run, state = kit["run"], kit["state"]
        chome = os.path.join(run, "codexhome")
        for d in (os.path.join(run, "home"), chome, os.path.join(run, "tmp")):
            os.makedirs(d, exist_ok=True)
        real_home = os.path.expanduser("~")
        with open(os.path.join(chome, "config.toml"), "w") as f:
            f.write(config_toml(run, real_root, live, real_home))
        with open(os.path.join(chome, "hooks.json"), "w") as f:
            json.dump(hooks_json(state, real["python3"]), f, indent=2)
        limits = ["the project is not trusted in the run, so its .codex/config.toml, rules and hooks do not load "
                  "(AGENTS.md and .agents/skills do)",
                  "Codex reports tokens, not cost; cost_usd is 0"]
        env = self._env(run)
        if "CODEX_API_KEY" not in env:
            auth = os.path.join(real_home, ".codex", "auth.json")
            if os.path.exists(auth):
                os.symlink(auth, os.path.join(chome, "auth.json"))
                limits.append("authenticated through the real ~/.codex/auth.json (linked, so a refresh lands there)")
        skills = self._skills(kit, env)
        with open(os.path.join(state, "skills.json"), "w") as f:
            json.dump({"skills": skills}, f)
        return {"env": env, "limits": limits}

    def _skills(self, kit: dict, env: dict) -> list | None:
        """Skills Codex lists to the model in this run's project, read from the rendered prompt
        without calling a model. None when the probe cannot run."""
        exe = self.binary()
        if not exe:
            return None
        try:
            p = subprocess.run([exe, "debug", "prompt-input", "x"], cwd=kit["proj"], env={**kit["env"], **env},
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return skills_in_prompt(p.stdout, self.skill_dirs) if p.returncode == 0 else None

    def probe(self, kit, command):
        try:
            p = subprocess.run([self.binary(), "sandbox", "-P", "verify", "-C", kit["proj"], "--", "sh", "-c", command],
                               cwd=kit["proj"], env=kit["env"], stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=60)
        except (OSError, TypeError, subprocess.TimeoutExpired) as e:
            return None if isinstance(e, TypeError) else (124, str(e))
        return p.returncode, p.stdout + p.stderr

    def command(self, prompt, kit, tasks):
        cmd = [self.binary() or "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
               "--dangerously-bypass-hook-trust", "-C", kit["proj"],
               "-o", os.path.join(kit["state"], "last-message.txt")]
        if tasks.get("model"):
            cmd += ["-m", tasks["model"]]
        return cmd + ["--", prompt]

    def parse(self, run_dir):
        return parse(run_dir, self.skill_dirs)
