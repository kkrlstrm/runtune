"""Build one verify run's world. Nothing here writes outside the run directory.

    <work>/<artifact>-<ts>/<arm>/<task>/r<i>/
      proj/    the agent's project: a copy-on-write clone of the real repository (a
               symlink farm where the filesystem cannot clone), so its CLAUDE.md, project
               hooks, skills and agents all load. The TREATMENT arm
               overlays the drafted artifact at its target path; CONTROL is the repo as it
               is. `.git`, `.runtune` (which holds the expected answers) and `.env*` are
               left out of the farm.
      state/   shim, hook, mocks.json, settings.json, and the effects/hook logs
      bin/     PATH wrappers routing python3/curl/psql/... through the shim

Three layers, each covering what the others cannot see:
  shims    answer mocked calls, refuse unmocked repo scripts and network tools (strict mode)
  hook     confines Write/Edit to the run dir (they run outside the OS sandbox), denies
           absolute-path interpreters and PATH rewrites
  sandbox  Claude Code's OS sandbox: no network (`strictAllowlist`, because in
           bypassPermissions mode hosts outside the allowlist are otherwise ALLOWED),
           unsandboxed retries off, secrets unreadable, startup refused if it is unavailable
The environment is an allowlist, and RUNTUNE_HOME points inside the run so a verify run
can never record itself into the user's own evidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("RUNTUNE_VERIFY_WORK") or os.path.join(os.path.expanduser("~"), ".cache", "runtune-verify")

SHIM_PY = ["python", "python3", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14"]
SHIM_NET = ["curl", "wget", "psql", "mysql", "gh", "glab", "ssh", "scp", "rsync", "claude", "aws",
            "gcloud", "kubectl", "fly", "flyctl", "osascript", "open", "xdg-open"]
SHIM_GIT = ["git"]
FARM_EXCLUDE = re.compile(r"^(?:\.git|\.runtune|\.env(?:\..*)?)$")

HOME_SECRETS = [".ssh", ".aws", ".config", ".netrc", ".pgpass", ".docker", ".kube", ".gnupg",
                ".claude", ".codex", ".cursor", ".gemini", ".runtune"]
SECRET_GLOBS = ["~/**/.env", "~/**/.env.*", "~/**/credentials*.json", "~/**/token.json",
                "~/**/*.pem", "~/**/id_rsa*", "~/**/id_ed25519*"]

ENV_KEEP = {"HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TMPDIR",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
            "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_REGION", "AWS_PROFILE",
            "CLOUD_ML_REGION", "ANTHROPIC_VERTEX_PROJECT_ID", "HTTPS_PROXY", "HTTP_PROXY",
            "NO_PROXY", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS"}


def real_binaries() -> dict:
    """Resolve real binaries once, from the caller's PATH (which has no shims)."""
    out = {n: shutil.which(n) for n in SHIM_PY + SHIM_NET + SHIM_GIT}
    out = {k: v for k, v in out.items() if v}
    if "python3" not in out:
        import sys
        out["python3"] = sys.executable
    return out


# --------------------------------------------------------------------- overlays
def narrow_grant(text: str, grant: list) -> str:
    """The agent file with its `tools:` line narrowed to `grant`. Same rule the promoter
    enforces: a revision may only REMOVE tools."""
    m = re.search(r"^tools:\s*(.+)$", text, re.M)
    if not m:
        raise ValueError("the agent file declares no tools line; there is no grant to narrow")
    old = {t.strip() for t in m.group(1).split(",")}
    if not set(grant) <= old:
        raise ValueError("a revision may only REMOVE tools from a grant")
    return text[:m.start()] + "tools: " + ", ".join(grant) + text[m.end():]


def treatment_files(art: dict, root: str) -> dict:
    """{repo-relative path: file content} that the treatment arm overlays: exactly what
    `apply` would write, computed without writing anything."""
    cand, prop = art["candidate"], art["candidate"].get("proposal", {})
    rel = os.path.relpath(art["target"], root)
    if rel.startswith(".."):
        raise ValueError(f"artifact target {art['target']} is outside --target-root {root}")
    if art["kind"] == "capability":
        if not prop.get("skill_md"):
            raise ValueError("this capability has no drafted SKILL.md")
        return {os.path.join(rel, "SKILL.md"): prop["skill_md"]}
    if art["kind"] == "subagent":
        if prop.get("revises"):
            with open(art["target"]) as f:
                return {rel: narrow_grant(f.read(), prop["grant"])}
        if prop.get("agent_md"):
            return {rel: prop["agent_md"]}
        raise ValueError("this sub-agent finding has no file to test (lint findings are fixed by hand)")
    raise ValueError(f"verify runs capabilities and sub-agents; a {cand['kind']} is checked by "
                     f"{'the replay gate' if cand['kind'] == 'constraint' else 'a model eval'}")


def _materialize(proj: str, real_root: str, rel: str) -> None:
    """Turn every symlinked ancestor of `rel` into a real directory of symlinks, so the leaf
    can be replaced without touching the real repository."""
    cur, real = proj, real_root
    for part in rel.split(os.sep)[:-1]:
        cur, real = os.path.join(cur, part), os.path.join(real, part)
        if os.path.islink(cur):
            os.unlink(cur)
            os.mkdir(cur)
            for child in os.listdir(real):
                if not FARM_EXCLUDE.match(child):
                    os.symlink(os.path.join(real, child), os.path.join(cur, child))
        elif not os.path.exists(cur):
            os.mkdir(cur)


def _place(proj: str, real_root: str, rel: str, content: str | None, mode: int = 0o644) -> None:
    _materialize(proj, real_root, rel)
    leaf = os.path.join(proj, rel)
    if os.path.islink(leaf) or os.path.isfile(leaf):
        os.unlink(leaf)
    elif os.path.isdir(leaf):
        shutil.rmtree(leaf)
    if content is not None:
        with open(leaf, "w") as f:
            f.write(content)
        os.chmod(leaf, mode)


def _clone(src: str, dst: str) -> bool:
    """Copy-on-write clone (APFS clonefile, Linux reflink). False if the filesystem can't."""
    import platform
    import subprocess
    flags = ["-cR"] if platform.system() == "Darwin" else ["-R", "--reflink=always"]
    r = subprocess.run(["cp", *flags, src, dst], capture_output=True)
    return r.returncode == 0


def build_farm(proj: str, real_root: str, overlay: dict) -> str:
    """The run's project. CLONE (default): a copy-on-write copy, so every tool sees real
    directories and a write cannot reach the real repository. A symlink farm was the first
    design: `find dir -type f` does not descend into a symlinked directory, and on 2026-10-08
    one control run saw an empty project and answered wrong because of it. LINK is the
    fallback where the filesystem cannot clone; the verify result records which was used.
    Returns "clone" or "link"."""
    os.makedirs(proj)
    names = [n for n in os.listdir(real_root) if not FARM_EXCLUDE.match(n)]
    mode = "clone" if os.environ.get("RUNTUNE_VERIFY_FARM", "clone") == "clone" else "link"
    if mode == "clone":
        for i, name in enumerate(names):
            if not _clone(os.path.join(real_root, name), os.path.join(proj, name)):
                for done in names[:i + 1]:
                    p = os.path.join(proj, done)
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) and not os.path.islink(p) \
                        else (os.path.lexists(p) and os.unlink(p))
                mode = "link"
                break
    if mode == "link":
        for name in names:
            os.symlink(os.path.join(real_root, name), os.path.join(proj, name))
    for rel, content in overlay.items():
        _place(proj, real_root, rel, content)
    return mode


# ------------------------------------------------------------------- the run kit
def deny_paths(root: str, live: dict | None) -> list:
    """Absolute paths no run may read: credential directories in the home, and the real
    repository's .runtune (expected answers), .git and .env files. A `live.read` entry
    lifts one of them."""
    home = os.path.expanduser("~")
    deny = [os.path.join(home, p) for p in HOME_SECRETS]
    deny += [os.path.join(root, ".runtune"), os.path.join(root, ".git")]
    deny += [os.path.join(root, n) for n in os.listdir(root) if re.match(r"^\.env(\..*)?$", n)]
    allow_read = [os.path.join(root, p) for p in (live or {}).get("read", [])]
    return [d for d in deny if d not in allow_read]


def env_for(run: str, bindir: str, live: dict | None) -> dict:
    keep = ENV_KEEP | set((live or {}).get("env", []))
    env = {k: v for k, v in os.environ.items() if k in keep or k.startswith("LC_")}
    env["PATH"] = f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
    env["TERM"] = "dumb"
    env["RUNTUNE_HOME"] = os.path.join(run, "state", "runtune-home")
    return env


def prepare(run: str, real_root: str, overlay: dict, tasks: dict, real: dict, host=None) -> dict:
    """Build proj/, state/ and bin/ for one run, then let the host write its own
    configuration (hosts/). `tasks` is the verify task file: its mocks, intercepts,
    passthrough patterns and live block shape the run."""
    if host is None:
        from .hosts.claude import Claude
        host = Claude()
    live = tasks.get("live") if tasks.get("_mode") == "live" else None
    proj, state, bindir = (os.path.join(run, d) for d in ("proj", "state", "bin"))
    farm = build_farm(proj, real_root, overlay)
    os.makedirs(state)
    os.makedirs(bindir)
    for fn in ("shim.py", "hook.py"):
        shutil.copy2(os.path.join(HERE, fn), os.path.join(state, fn))
    for fn in ("effects.jsonl", "hook.jsonl"):
        open(os.path.join(state, fn), "w").close()
    mocks = []
    for i, m in enumerate(tasks.get("mocks", [])):
        m = dict(m)
        m.setdefault("id", f"mock-{i + 1}")
        mocks.append(m)
    cfg = {"mode": "live" if live is not None else "strict", "real": real, "repo": real_root,
           "project": proj, "mocks": mocks, "passthrough": tasks.get("passthrough", []),
           "repo_modules": tasks.get("repo_modules", [])}
    with open(os.path.join(state, "mocks.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    evars = f'RUNTUNE_VERIFY_STATE="{state}"'
    for name in SHIM_PY + SHIM_NET + SHIM_GIT:
        w = os.path.join(bindir, name)
        with open(w, "w") as f:
            f.write(f'#!/bin/sh\nexec env {evars} "{real["python3"]}" "{state}/shim.py" {name} "$@"\n')
        os.chmod(w, 0o755)
    for rel in tasks.get("intercept", []):      # repo executables called by path
        _place(proj, real_root, rel,
               f'#!/bin/sh\nexec env {evars} "{real["python3"]}" "{state}/shim.py" "@{rel}" "$@"\n', 0o755)
    kit = {"run": run, "proj": proj, "state": state, "bin": bindir, "env": env_for(run, bindir, live),
           "farm": farm, "host": host.name}
    got = host.configure(kit, tasks, real, real_root, live)
    kit["env"].update(got.get("env") or {})
    kit["limits"] = list(got.get("limits") or [])
    return kit
