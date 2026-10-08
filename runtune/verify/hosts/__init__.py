"""The agent hosts verify can run headless. One module per host; everything else in verify
is host-neutral.

A host supplies five things:

  binary()      where its CLI is, or None
  configure()   write the run's host configuration into state/ (sandbox, hook wiring,
                MCP off, an isolated config home); return extra env and the facts the
                result should record about containment
  command()     the argv for one headless prompt
  parse()       its own event stream -> trace.RunRecord, with tool calls renamed to the
                shared vocabulary (Bash, Read, Write, Edit, Skill, Agent, ...) so that a
                measure such as {"bash": regex} means the same on every host
  skill_dirs    where it loads skills from, relative to the project, so `static` can say
                when an artifact's target is somewhere the chosen host will never look

What a host cannot contain is stated, not hidden: `configure` returns `limits`, and they go
into the verify result next to the verdict.
"""

from __future__ import annotations

import os
import shutil

from .. import trace


class Host:
    name = ""
    exe = ()                 # names or absolute paths to try, in order
    skill_dirs: tuple = ()
    agent_dirs: tuple = ()
    tools: frozenset = frozenset()      # tool names a sub-agent file may grant

    def binary(self) -> str | None:
        env = os.environ.get(f"RUNTUNE_VERIFY_{self.name.upper()}_BIN")
        for c in ((env,) if env else ()) + tuple(self.exe):
            c = os.path.expanduser(c)
            p = c if os.path.isabs(c) else shutil.which(c)
            if p and os.access(p, os.X_OK):
                return p
        return None

    def ready(self) -> str | None:
        """Why this host cannot run here (missing CLI or credentials), or None."""
        if not self.binary():
            return f"{self.name}'s CLI was not found (tried {', '.join(self.exe)}; or set RUNTUNE_VERIFY_{self.name.upper()}_BIN)"
        return None

    def version(self) -> str:
        import subprocess
        try:
            p = subprocess.run([self.binary(), "--version"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            return (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else "unknown"
        except (OSError, TypeError, subprocess.TimeoutExpired):
            return "unknown"

    def probe(self, kit: dict, command: str):
        """Run one shell command under this run's sandbox WITHOUT a model -> (exit code,
        output), or None if the host offers no way to. The selftest uses it for escapes a
        model declines to attempt."""
        return None

    def configure(self, kit: dict, tasks: dict, real: dict, real_root: str, live: dict | None) -> dict:
        """-> {"env": {...}, "limits": [...]}"""
        raise NotImplementedError

    def command(self, prompt: str, kit: dict, tasks: dict) -> list:
        raise NotImplementedError

    def parse(self, run_dir: str) -> trace.RunRecord:
        raise NotImplementedError

    def reads(self, rel_target: str) -> bool:
        """Would this host load an artifact written at `rel_target`?"""
        dirs = self.skill_dirs + self.agent_dirs
        return any(rel_target == d or rel_target.startswith(d.rstrip("/") + "/") for d in dirs)


def get(name: str) -> Host:
    from . import antigravity, claude, codex, cursor
    hosts = {"claude": claude.Claude, "codex": codex.Codex, "cursor": cursor.Cursor,
             "antigravity": antigravity.Antigravity}
    if name not in hosts:
        raise ValueError(f"unknown host {name!r}; verify runs {', '.join(sorted(hosts))}")
    return hosts[name]()


NAMES = ("claude", "codex", "cursor", "antigravity")
