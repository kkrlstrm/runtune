"""Antigravity CLI: `agy -p --output-format stream-json`.

Built from Antigravity's documented formats (antigravity.google/docs/cli/headless, /sandbox,
/permissions, /hooks) and not yet run on a machine with `agy` installed; `runtune verify
--selftest --host antigravity` must pass before a verdict counts (see verify.run). The
Antigravity app has no headless mode; its language server's `-headless` flag and `agentapi`
are internal and are not used.

Each run gets its own HOME, so ~/.gemini (global rules, MCP config, hooks, settings) is out.
Auth is GEMINI_API_KEY with `modelProvider: "gemini"` in the run's settings.

Containment:
  - the terminal sandbox (`--sandbox`, macOS sandbox-exec) for shell commands: writes to
    the workspace, sensitive files and non-approved domains blocked
  - permission deny rules for the file tools, which the terminal sandbox does not cover
    (workspace files are otherwise auto-allowed)
  - the hook, from the run's ~/.gemini/config/hooks.json
How `--dangerously-skip-permissions` (needed headless) interacts with deny rules, and whether
a failing hook blocks, are undocumented: the selftest is what answers them on a given version.
`result.status` has reported SUCCESS with an empty response (agy issue 1065), so a run with
no answer is not counted as finished.
"""

from __future__ import annotations

import json
import os

from .. import sandbox, trace
from . import Host

_TOOL = {"run_command": ("Bash", "CommandLine", "command"), "view_file": ("Read", "AbsolutePath", "file_path"),
         "write_to_file": ("Write", "TargetFile", "file_path"),
         "replace_file_content": ("Edit", "TargetFile", "file_path"),
         "multi_replace_file_content": ("Edit", "TargetFile", "file_path"),
         "grep_search": ("Grep", "Query", "pattern"), "search_web": ("WebSearch", "query", "query"),
         "read_url_content": ("WebFetch", "Url", "url"), "invoke_subagent": ("Agent", None, None)}


def settings_json(root: str, live: dict | None) -> dict:
    deny = [f"read_file({p})" for p in sandbox.deny_paths(root, live)]
    deny += [f"write_file({root})"] + [f"read_file({g})" for g in sandbox.SECRET_GLOBS]
    doc = {"enableTerminalSandbox": True,
           "permissions": {"deny": deny, "allow": [f"read_url({d})" for d in (live or {}).get("network", [])]}}
    if os.environ.get("GEMINI_API_KEY"):
        doc["modelProvider"] = "gemini"
    return doc


def hooks_json(state: str, python: str) -> dict:
    cmd = f'"{python}" "{state}/hook.py" antigravity'
    return {"runtune-verify": {"enabled": True, "PreToolUse": [{"matcher": ".*",
                                                                "hooks": [{"command": cmd, "timeout": 15}]}]}}


def _use(idx, info: dict) -> dict:
    name = info.get("name") or "?"
    params = info.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {"raw": params}
    shared, src, dst = _TOOL.get(name, (name, None, None))
    inp = dict(params)
    if src and src in params:
        inp[dst] = str(params[src]).strip().strip('"')
    if shared == "Agent":
        subs = params.get("Subagents") or []
        if subs and isinstance(subs[0], dict):
            inp["subagent_type"] = subs[0].get("TypeName") or subs[0].get("Role")
    return {"id": f"step-{idx}", "name": shared, "input": inp}


PROJECT_OVERRIDES = tuple(f"{d}/mcp_config.json" for d in (".agents", ".agent", "_agents", "_agent"))


def _from_transcript(r: trace.RunRecord, run_dir: str) -> None:
    """Every run also writes the app's transcript under its own HOME. When the event stream
    is empty or ends without an answer (agy issues 408, 1065), read the run from it instead."""
    import glob
    from ...record import turns
    paths = glob.glob(os.path.join(run_dir, "home", ".gemini", "antigravity*", "brain", "*",
                                   ".system_generated", "logs", "transcript.jsonl"))
    if not paths:
        return
    sid = paths[0].split(os.sep)[-4]
    steps = turns._antigravity(sid, root=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(paths[0])))))
    if not r.uses:
        names = {}
        for o in trace._jsonl(paths[0]):
            for i, tc in enumerate(o.get("tool_calls") or []):
                if isinstance(tc, dict):
                    names[f"{o.get('step_index')}:{i}"] = tc
        for st in steps:
            if st[0] == "call" and st[1] in names:
                tc = names[st[1]]
                r.uses.append({**_use(st[1], {"name": tc.get("name"), "parameters": tc.get("args")}), "id": st[1]})
            elif st[0] == "output":
                r.results[st[1]] = st[2]
    answers = [st[1] for st in steps if st[0] == "answer" and st[1].strip()]
    if answers and not r.final.strip():
        r.final = answers[-1]
        r.started = True
        r.finished, r.end_reason = True, ""
        r.result = r.result or {"source": "transcript"}


def parse(run_dir: str, skill_dirs=()) -> trace.RunRecord:
    r = trace.RunRecord(run_dir)
    steps, texts = {}, {}
    for ev in trace._jsonl(os.path.join(run_dir, "trace.jsonl")):
        kind = ev.get("event")
        if kind == "init":
            r.started, r.init = True, ev
            r.model = (ev.get("init") or {}).get("model")
        elif kind == "step_update":
            su = ev.get("step_update") or {}
            idx = su.get("step_index")
            if su.get("step_type") == "agent_response" and su.get("text_delta"):
                texts[idx] = texts.get(idx, "") + su["text_delta"]
            if su.get("step_type") == "tool" and su.get("state") == "DONE" and su.get("tool_info"):
                steps[idx] = su["tool_info"]
        elif kind == "result":
            res = ev.get("result") or {}
            r.result = res
            r.final = res.get("response") or ""
            r.turns = res.get("num_turns")
            r.tokens = {k: int(v or 0) for k, v in (res.get("usage") or {}).items() if isinstance(v, (int, float))}
            r.finished = res.get("status") == "SUCCESS"
            r.end_reason = "" if r.finished else f"{res.get('status')}: {res.get('error') or ''}".strip(": ")
    for idx in sorted(steps, key=lambda i: (i is None, i)):
        info = steps[idx]
        u = _use(idx, info)
        r.uses.append(u)
        err = info.get("error") or {}
        r.results[u["id"]] = (info.get("output") or "") + (f"\n{err.get('message')}" if err.get("message") else "")
    if not r.final and texts:
        r.final = texts[max(k for k in texts if k is not None)] if any(k is not None for k in texts) else ""
    if r.finished and not r.final.strip():
        r.finished, r.end_reason = False, "SUCCESS with an empty response (a known agy defect)"
    if not r.final.strip():
        _from_transcript(r, run_dir)
    r.mcp = sorted({u["name"].split("__")[1] for u in r.uses if u["name"].startswith("mcp__")})
    trace.skill_reads(r, skill_dirs)
    return trace.harness_logs(r)


class Antigravity(Host):
    name = "antigravity"
    exe = ("agy", "~/.local/bin/agy", "~/.antigravity-cli/bin/agy")
    skill_dirs = (".agents/skills", ".agent/skills", "_agents/skills", "_agent/skills")
    agent_dirs = ()
    tools = frozenset()

    def ready(self):
        if not self.binary():
            return ("the Antigravity CLI is not installed (`agy`; https://antigravity.google/docs/cli/install), "
                    "or set RUNTUNE_VERIFY_ANTIGRAVITY_BIN. The Antigravity app has no headless mode")
        if not os.environ.get("GEMINI_API_KEY"):
            return "set GEMINI_API_KEY: each run has its own HOME, so the CLI's keychain login is not used"
        return None

    def configure(self, kit, tasks, real, real_root, live):
        run, state = kit["run"], kit["state"]
        home = os.path.join(run, "home")
        for d in (os.path.join(home, ".gemini", "antigravity-cli"), os.path.join(home, ".gemini", "config"),
                  os.path.join(run, "tmp")):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(home, ".gemini", "antigravity-cli", "settings.json"), "w") as f:
            json.dump(settings_json(real_root, live), f, indent=2)
        with open(os.path.join(home, ".gemini", "config", "hooks.json"), "w") as f:
            json.dump(hooks_json(state, real["python3"]), f, indent=2)
        removed = [rel for rel in PROJECT_OVERRIDES if os.path.lexists(os.path.join(kit["proj"], rel))]
        for rel in removed:
            # through _place: in a symlink farm the parent directory is a link into the REAL
            # repository, and a plain unlink would delete the real file (CI caught it on Linux)
            sandbox._place(kit["proj"], real_root, rel, None)
        env = {"HOME": home, "TMPDIR": os.path.join(run, "tmp")}
        if os.environ.get("GEMINI_API_KEY"):
            env["GEMINI_API_KEY"] = os.environ["GEMINI_API_KEY"]
        return {"env": env, "limits": [
            "Antigravity documents no environment filter: GEMINI_API_KEY is visible to the agent's shell",
            "the terminal sandbox covers shell commands only; file tools rely on permission deny rules",
            "Antigravity reports neither loaded skills nor cost; skill use is counted from reads of SKILL.md"]
            + [f"removed the project's {rel} from the run's copy: it would start MCP servers" for rel in removed]}

    def command(self, prompt, kit, tasks):
        cmd = [self.binary() or "agy", "-p", prompt, "--output-format", "stream-json",
               "--dangerously-skip-permissions", "--sandbox",
               "--print-timeout", f"{int(tasks.get('timeout_seconds', 600))}s"]
        if tasks.get("model"):
            cmd += ["--model", tasks["model"]]
        return cmd

    def parse(self, run_dir):
        return parse(run_dir, self.skill_dirs)
