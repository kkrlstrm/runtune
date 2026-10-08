"""verify: run a staged artifact before anyone applies it.

The derivers' replay gate checks a proposal against recorded attempts without executing
anything. That cannot catch a skill that names a command that does not exist (RunTune
shipped one), or a skill that agents never act on. `verify` executes:

  static     free, no model. Capability: every command the drafted skill names resolves.
             Sub-agent: the file parses, its tools are real tool names, a revision only
             removes tools.
  arms       CONTROL (the repository as it is) against TREATMENT (the repository with the
             drafted artifact overlaid at its target), on tasks a person writes, with the
             host run headless in a contained copy of the repository (see sandbox.py).
             Arms are interleaved within each task x repetition block.

The verdict uses Fisher's exact test, and the run count is checked against the best p it
can reach BEFORE anything is spent:

  pass          treatment did the intended thing significantly more often than control,
                and was not significantly less correct (no measure: every treatment run
                correct and none worse than control)
  no-effect     treatment did not do the intended thing more often than control
  fail          treatment answered significantly fewer tasks correctly, or static failed
  inconclusive  the difference did not reach significance, or too many runs were invalid

Only `pass` satisfies `apply --eval`, and only for the exact draft that was run against an
unchanged target (`check`). `init` drafts tasks from the sessions behind the evidence when
their transcripts still exist (seed.py); a person reviews every one before it can run.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ..lifecycle import ledger
from ..lifecycle.store import Workspace, file_digest
from . import hosts, sandbox, seed, stats, trace

SCHEMA = 1
DEFAULT_RUNS = stats.runs_for(0.05)          # 4: the fewest that can reach p < 0.05
_PLACEHOLDER = re.compile(r"<[^<>\n]{2,60}>")


class VerifyError(Exception):
    """A verify that cannot run as asked. Never turned into a verdict."""


def proposal_digest(art: dict) -> str:
    """Identity of WHAT was verified: the parts of the proposal that apply would write."""
    p = art["candidate"].get("proposal", {})
    body = {"kind": art["kind"], "target": os.path.basename(art["target"]),
            **{k: p.get(k) for k in ("skill_md", "agent_md", "grant", "revises")}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _dir(ws: Workspace, aid: str) -> str:
    d = os.path.join(ws.root, "verify", aid)
    os.makedirs(d, exist_ok=True)
    return d


def _art(ws: Workspace, aid: str) -> dict:
    art = ws.get(aid)
    if not art:
        raise VerifyError(f"no staged artifact {aid}; `runtune stage {aid}` first")
    if art["state"] != "staged":
        raise VerifyError(f"{aid} is {art['state']}; verify runs before apply")
    return art


# ----------------------------------------------------------------------- static
def _skill_commands(skill_md: str) -> list:
    """Commands a skill tells agents to run: backticked list items and fenced shell lines."""
    cmds = re.findall(r"^\s*[-*]\s+`([^`]+)`", skill_md, re.M)
    for block in re.findall(r"```(?:bash|sh|shell)?\n(.*?)```", skill_md, re.S):
        cmds += [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    cmds = [re.sub(r"\s*(?:…|\.\.\.)\s*$", "", c).strip() for c in cmds]
    # `db.query_master()` names a function the CLI must cover, not a command to run
    return [c for c in cmds if not re.fullmatch(r"[\w.]+\([^)]*\)", c)]


def _resolves(cmd: str, root: str) -> tuple:
    toks = cmd.split()
    if not toks:
        return False, "empty command"
    head = toks[0]
    if head in ("python", "python3") and len(toks) > 1 and not toks[1].startswith("-"):
        head = toks[1]
    if "/" in head or head.endswith(".py"):
        p = head if os.path.isabs(head) else os.path.join(root, head)
        if not os.path.exists(p):
            return False, f"`{head}` does not exist under {root}"
        if not head.endswith(".py") and not os.access(p, os.X_OK):
            return False, f"`{head}` exists but is not executable"
        return True, f"`{head}` exists"
    if shutil.which(head):
        return True, f"`{head}` is on PATH"
    return False, f"`{head}` is neither a repository path nor on PATH"


def static(ws: Workspace, aid: str, root: str = ".", host: str | None = None) -> dict:
    art = _art(ws, aid)
    prop = art["candidate"].get("proposal", {})
    if host is None:
        tp = os.path.join(_dir(ws, aid), "tasks.json")
        try:
            host = json.load(open(tp)).get("host") if os.path.exists(tp) else None
        except ValueError:
            host = None
        host = host or _host_for(art)
    h = hosts.get(host)
    checks = []
    try:
        files = sandbox.treatment_files(art, root)
        checks.append(("overlay", True, f"{len(files)} file(s) to test: {', '.join(files)}"))
    except (ValueError, OSError) as e:
        checks.append(("overlay", False, str(e)))
        files = {}
    if art["kind"] in ("capability", "subagent"):
        rel = os.path.relpath(art["target"], os.path.abspath(root))
        checks.append((f"{host} loads the target", h.reads(rel),
                       f"{rel} is under {', '.join(h.skill_dirs + h.agent_dirs)}" if h.reads(rel) else
                       f"{host} loads skills from {', '.join(h.skill_dirs)} and agents from "
                       f"{', '.join(h.agent_dirs) or 'nowhere'}; an artifact at {rel} would never reach it "
                       f"(set targets in .runtune/authority.json, or verify on the host it is written for)"))
    if art["kind"] == "capability" and prop.get("skill_md"):
        md = prop["skill_md"]
        if "DRAFT: no CLI exists yet" in md:
            checks.append(("commands", False, "the skill is a draft with no CLI to call; build the CLI and "
                                              "list its real commands first"))
        cmds = _skill_commands(md)
        if not cmds:
            checks.append(("commands", False, "the skill names no command to check"))
        for c in cmds:
            ok, why = _resolves(c, root)
            checks.append((f"command: {c[:60]}", ok, why))
        nudge = (prop.get("companion_nudge") or {}).get("any") or []
        for rx in nudge:
            try:
                re.compile(rx)
                checks.append(("nudge regex", True, "compiles"))
            except re.error as e:
                checks.append(("nudge regex", False, str(e)))
    if art["kind"] == "subagent":
        for rel, text in files.items():
            name = re.search(r"^name:\s*(.+)$", text, re.M)
            tools = re.search(r"^tools:\s*(.+)$", text, re.M)
            checks.append(("frontmatter", bool(name and tools), "name and tools present" if name and tools
                           else "missing name: or tools: line"))
            if tools:
                listed = [t.strip() for t in tools.group(1).split(",") if t.strip()]
                unknown = [t for t in listed if t not in h.tools and not t.startswith("mcp__")]
                checks.append(("tool names", not unknown, f"unknown: {unknown}" if unknown
                               else f"{len(listed)} known tool(s)"))
    ok = all(c[1] for c in checks)
    out = {"ok": ok, "checks": [{"name": n, "ok": k, "detail": d} for n, k, d in checks]}
    with open(os.path.join(_dir(ws, aid), "static.json"), "w") as f:
        json.dump(out, f, indent=2)
    return out


# ------------------------------------------------------------------------ tasks
def _host_for(art: dict) -> str:
    """The host whose directories the artifact's target sits in (claude if none)."""
    for name in hosts.NAMES:
        h = hosts.get(name)
        if any(f"/{d}/" in f"/{art['target']}/" for d in h.skill_dirs + h.agent_dirs):
            return name
    return "claude"


def _default_measure(art: dict) -> dict | None:
    cand, prop = art["candidate"], art["candidate"].get("proposal", {})
    if art["kind"] == "capability" and cand.get("key", "").startswith("inline|"):
        heads = sorted({" ".join(c.split()[:2]) for c, _ in cand.get("numbers", {}).get("cli_observed", [])})
        old = (prop.get("companion_nudge") or {}).get("any", [None])[0]
        new = ("(?:^|[\\s;&|(])(?:\\./)?(?:" + "|".join(re.escape(h) for h in heads) + ")\\b") if heads else None
        return {"new": {"bash": new} if new else None, "old": {"bash": old} if old else None}
    if art["kind"] == "subagent" and not art["candidate"]["proposal"].get("revises"):
        name = re.search(r"^name:\s*(.+)$", prop.get("agent_md", ""), re.M)
        return {"new": {"agent": name.group(1).strip()} if name else None, "old": None}
    return None


def init_tasks(ws: Workspace, aid: str, host: str | None = None) -> str:
    art = _art(ws, aid)
    path = os.path.join(_dir(ws, aid), "tasks.json")
    if os.path.exists(path):
        raise VerifyError(f"{path} exists; edit it (init never overwrites a task file)")
    cand = art["candidate"]
    measure = _default_measure(art)
    drafted, notes = seed.tasks(cand.get("samples") or cand.get("correction") or [],
                                pattern=((measure or {}).get("old") or {}).get("bash"))
    doc = {
        "_readme": ("Write 3+ tasks a real user would give, phrased as they would, whose answer you know. "
                    "Tasks marked `reviewed: false` were drafted from the sessions behind the evidence: "
                    "the prompt is what the person typed, `expect` is a guess from what the command printed "
                    "and the agent answered (see from_evidence). Read each one, fix or delete it, then set "
                    "reviewed: true. "
                    "`measure.new` is what the artifact should make agents do, `measure.old` what it "
                    "replaces (regex over Bash, or {skill|agent|tool: name}). Replace every <placeholder>. "
                    "Strict mode (default) has no network: give `mocks` for every repo script or network "
                    "call the tasks need, or use `live` (needs --live --reason)."),
        "artifact": aid,
        "host": host or _host_for(art),
        "runs": DEFAULT_RUNS,
        "model": None,
        "max_turns": 25,
        "timeout_seconds": 600,
        "measure": measure,
        "exercise": list(dict.fromkeys(s.get("text", "") for s in cand.get("correction", [])))[:5],
        "tasks": drafted or [{"name": "<short-name>", "prompt": "<what a user would type>",
                              "expect": {"contains": "<substring of a correct answer>"}}],
        "_drafting_notes": notes,
        "mocks": [],
        "intercept": [],
        "passthrough": [],
        "repo_modules": [cand["key"].split("|", 1)[1]] if cand.get("key", "").startswith("inline|") else [],
        "live": {"network": [], "env": [], "read": []},
    }
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)
    return path


def load_tasks(path: str, aid: str) -> dict:
    with open(path) as f:
        doc = json.load(f)
    if doc.get("artifact") != aid:
        raise VerifyError(f"{path} is for {doc.get('artifact')}, not {aid}")
    if doc.get("host", "claude") not in hosts.NAMES:
        raise VerifyError(f"unknown host {doc.get('host')!r}; verify runs {', '.join(hosts.NAMES)}")
    if not doc.get("tasks"):
        raise VerifyError(f"{path} has no tasks")
    # a prompt drafted from a real session may contain <angle brackets> of its own; the
    # `reviewed` flag, not the placeholder scan, is what holds those for a person
    scan = [{k: v for k, v in t.items() if not (k == "prompt" and t.get("from_evidence")) and k != "from_evidence"}
            for t in doc["tasks"]]
    text = json.dumps({"tasks": scan, "measure": doc.get("measure"), "mocks": doc.get("mocks")})
    left = sorted(set(_PLACEHOLDER.findall(text)))
    if left:
        raise VerifyError(f"{path} still has placeholders: {', '.join(left[:6])}")
    for t in doc["tasks"]:
        if not t.get("name") or not t.get("prompt"):
            raise VerifyError("every task needs a name and a prompt")
        if t.get("reviewed") is False:
            raise VerifyError(f"task {t['name']} was drafted from evidence and not reviewed: read its prompt "
                              "and expectation (and from_evidence), then set \"reviewed\": true")
    for side in ("new", "old"):
        spec = (doc.get("measure") or {}).get(side)
        if spec and "bash" in spec:
            re.compile(spec["bash"])
    for m in doc.get("mocks", []):
        re.compile(m["match"])
    return doc


# ---------------------------------------------------------------------- running
def launch(cmd: list, cwd: str, env: dict, run_dir: str, timeout: int) -> tuple:
    """Run one headless session; its stdout is the host's event stream. -> (exit code,
    error or None). Kills the whole process group on timeout: a left-over child keeps
    spending."""
    with open(os.path.join(run_dir, "trace.jsonl"), "w") as out, \
         open(os.path.join(run_dir, "stderr.txt"), "w") as err:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             start_new_session=True)
        try:
            return p.wait(timeout=timeout), None
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                    p.wait(timeout=10)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
            return p.returncode, f"timed out after {timeout}s"


launch_claude = launch   # older name


def claude_cmd(prompt: str, settings: str, max_turns: int, model: str | None) -> list:
    return hosts.get("claude").command(prompt, {"settings": settings}, {"max_turns": max_turns, "model": model})


def _one(job, art, tasks, real_root, base, real, launcher, host) -> dict:
    arm, task, rep, overlay = job
    run_dir = os.path.join(base, arm, re.sub(r"[^\w.-]+", "-", task["name"]), f"r{rep}")
    kit = sandbox.prepare(run_dir, real_root, overlay, tasks, real, host)
    cmd = host.command(task["prompt"], kit, tasks)
    t0 = time.time()
    code, err = launcher(cmd, kit["proj"], kit["env"], run_dir, int(tasks.get("timeout_seconds", 600)))
    rec = host.parse(run_dir)
    if not err:
        if not rec.started:
            err = f"the host never started (exit {code}); see stderr.txt"
        elif rec.mcp:
            err = f"containment: MCP servers present {rec.mcp}"
        elif not rec.finished:
            err = f"run ended: {rec.end_reason or f'no end event (exit {code})'}"
    skill_name = os.path.basename(art["target"]) if art["kind"] == "capability" else None
    if not err and skill_name and rec.skills is not None:
        if arm == "treatment" and skill_name not in rec.skills:
            err = f"treatment: skill {skill_name} did not load in the host"
        if arm == "control" and skill_name in rec.skills and art.get("base_digest") is None:
            err = f"control: skill {skill_name} is present but the target did not exist at staging"
    m = tasks.get("measure") or {}
    seal = [f"unmocked: {e.get('cmd')}" for e in rec.unmocked]
    seal += [f"denied: {h.get('why') or h.get('path')}" for h in rec.denied]
    seal += trace.peeked(rec, run_dir, real_root)
    return {"arm": arm, "task": task["name"], "rep": rep, "error": err, "sealed": not seal, "seal": seal[:5],
            "correct": trace.correct(rec, task.get("expect")),
            "new": trace.matches(rec, m.get("new")), "old": trace.matches(rec, m.get("old")),
            "turns": rec.turns, "cost_usd": rec.cost_usd, "tokens": rec.tokens, "model": rec.model, "wall_s": round(time.time() - t0, 1),
            "answer": rec.answer[:240], "bash": [c[:200] for c in rec.bash][:20], "farm": kit["farm"],
            "skill_load": "reported" if rec.skills is not None else "not reported by the host",
            "limits": kit.get("limits", [])}


def _arm_summary(rows: list, has_measure: bool) -> dict:
    valid = [r for r in rows if not r["error"] and r["sealed"]]
    graded = [r for r in valid if r["correct"] is not None]
    adopted = [r for r in valid if r["new"] and not r["old"]] if has_measure else []
    return {"runs": len(rows), "valid": len(valid),
            "errors": sum(bool(r["error"]) for r in rows),
            "unsealed": sum(not r["sealed"] for r in rows if not r["error"]),
            "correct": sum(bool(r["correct"]) for r in graded), "graded": len(graded),
            "adopted": len(adopted), "cost_usd": round(sum(r["cost_usd"] for r in rows), 4)}


def _p(p: float) -> str:
    return "<0.001" if p < 0.001 else f"={p:.3f}"


def verdict(c: dict, t: dict, has_measure: bool, alpha: float = 0.05) -> tuple:
    reasons = []
    for name, a in (("control", c), ("treatment", t)):
        if a["valid"] < max(1, a["runs"] // 2):
            return "inconclusive", [f"{name}: only {a['valid']} of {a['runs']} runs were valid "
                                    f"({a['errors']} errors, {a['unsealed']} touched unmocked or outside paths)"]
    if t["unsealed"]:
        reasons.append(f"{t['unsealed']} treatment run(s) reached outside the case and were excluded")
    p_corr = stats.fisher(t["correct"], t["graded"], c["correct"], c["graded"])
    lower = False
    if t["graded"] and c["graded"] and t["correct"] / t["graded"] < c["correct"] / c["graded"]:
        if p_corr < alpha:
            return "fail", reasons + [f"treatment answered fewer tasks correctly: {t['correct']}/{t['graded']} "
                                      f"vs {c['correct']}/{c['graded']} (p{_p(p_corr)})"]
        lower = True
        reasons.append(f"treatment correctness lower, not significant: {t['correct']}/{t['graded']} vs "
                       f"{c['correct']}/{c['graded']} (p{_p(p_corr)})")
    if has_measure:
        p = stats.fisher(t["adopted"], t["valid"], c["adopted"], c["valid"])
        line = f"took the new path: treatment {t['adopted']}/{t['valid']} vs control {c['adopted']}/{c['valid']} (p{_p(p)})"
        if t["adopted"] / t["valid"] <= c["adopted"] / c["valid"]:
            return "no-effect", reasons + [line]
        if p >= alpha:
            return "inconclusive", reasons + [line + f"; best reachable at this n: p={stats.best_p(min(t['valid'], c['valid'])):.3f}"]
        if lower:
            return "inconclusive", reasons + [line + "; but correctness was lower"]
        return "pass", reasons + [line]
    if t["graded"] and t["correct"] == t["graded"] and t["correct"] / t["graded"] >= (c["correct"] / c["graded"] if c["graded"] else 0):
        return "pass", reasons + [f"no measure: every treatment run correct ({t['correct']}/{t['graded']}), "
                                  f"control {c['correct']}/{c['graded']}; this shows no harm, not an effect"]
    return "inconclusive", reasons + ["no measure, and not every treatment run was correct"]


def run(ws: Workspace, aid: str, root: str = ".", tasks_path: str | None = None, runs: int | None = None,
        jobs: int = 2, live_reason: str | None = None, launcher=None, keep: bool = False,
        log=print) -> dict:
    art = _art(ws, aid)
    root = os.path.abspath(root)
    tasks_path = tasks_path or os.path.join(_dir(ws, aid), "tasks.json")
    if not os.path.exists(tasks_path):
        raise VerifyError(f"no task file; run `runtune verify {aid} --init` and fill it in")
    tasks = load_tasks(tasks_path, aid)
    host = hosts.get(tasks.get("host", "claude"))
    if launcher is None:
        problem = host.ready()
        if problem:
            raise VerifyError(problem)
        if host.name != "claude":
            from . import selftest
            ok, why = selftest.passed(host)
            if not ok:
                raise VerifyError(f"{why}. {host.name}'s containment is configuration this version may not honor: "
                                  f"run `runtune verify --selftest --host {host.name}` first")
    st = static(ws, aid, root, host.name)
    if live_reason is not None:
        if not live_reason.strip():
            raise VerifyError("--live widens what the run may reach; it needs --reason")
        tasks["_mode"] = "live"
    n = runs or int(tasks.get("runs") or DEFAULT_RUNS)
    best = stats.best_p(n)
    log(f"verify {aid}: {len(tasks['tasks'])} task(s) × 2 arms × {n} runs = {2 * n * len(tasks['tasks'])} "
        f"headless runs; best reachable p per task at n={n} is {best:.3f}"
        + ("" if best < 0.05 else f" (cannot reach 0.05; use --runs {stats.runs_for()} or more)"))
    if not st["ok"]:
        bad = [c for c in st["checks"] if not c["ok"]]
        log("static checks failed: " + "; ".join(f"{c['name']}: {c['detail']}" for c in bad))
    treatment = sandbox.treatment_files(art, root) if st["ok"] else {}
    work = os.path.realpath(sandbox.WORK)
    if work.startswith(root + os.sep):
        raise VerifyError(f"the verify work dir {work} is inside the repository; set RUNTUNE_VERIFY_WORK")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(work, f"{aid}-{ts}")
    real = sandbox.real_binaries()
    rng = random.Random(f"{aid}{ts}")
    jobs_list = []
    for rep in range(1, n + 1):
        for task in tasks["tasks"]:
            arms = [("control", {}), ("treatment", treatment)]
            rng.shuffle(arms)
            jobs_list += [(arm, task, rep, ov) for arm, ov in arms]
    rows = []
    if st["ok"]:
        with ThreadPoolExecutor(max(1, jobs)) as ex:
            for row in ex.map(lambda j: _one(j, art, tasks, root, base, real, launcher or launch, host), jobs_list):
                log(f"  {row['arm']:<9} {row['task']:<24} r{row['rep']}  "
                    + ("ERROR " + row["error"] if row["error"] else
                       f"correct={row['correct']} new={row['new']} old={row['old']}"
                       + ("" if row["sealed"] else f"  UNSEALED {row['seal'][:1]}")))
                rows.append(row)
    has_measure = bool((tasks.get("measure") or {}).get("new"))
    c = _arm_summary([r for r in rows if r["arm"] == "control"], has_measure)
    t = _arm_summary([r for r in rows if r["arm"] == "treatment"], has_measure)
    if not st["ok"]:
        v, reasons = "fail", ["static checks failed; no runs were made"]
    else:
        v, reasons = verdict(c, t, has_measure)
    per_task = {}
    for task in tasks["tasks"]:
        tc = _arm_summary([r for r in rows if r["arm"] == "control" and r["task"] == task["name"]], has_measure)
        tt = _arm_summary([r for r in rows if r["arm"] == "treatment" and r["task"] == task["name"]], has_measure)
        per_task[task["name"]] = {"control": tc, "treatment": tt}
    result = {"schema": SCHEMA, "artifact": aid, "kind": art["kind"], "verdict": v, "reasons": reasons,
              "proposal_digest": proposal_digest(art), "base_digest": file_digest(art["target"]),
              "static": st, "host": host.name, "model": tasks.get("model") or next((r["model"] for r in rows if r["model"]), None),
              "mode": tasks.get("_mode", "strict"), "live_reason": live_reason,
              "live": tasks.get("live") if live_reason else None,
              "runs_per_arm": n, "best_p": best, "measure": tasks.get("measure"),
              "arms": {"control": c, "treatment": t}, "per_task": per_task,
              "cost_usd": round(c["cost_usd"] + t["cost_usd"], 4), "created": ts, "rows": rows,
              "tokens": {k: sum((r.get("tokens") or {}).get(k, 0) for r in rows)
                         for k in sorted({k for r in rows for k in (r.get("tokens") or {})})},
              "farm": sorted({r.get("farm") for r in rows if r.get("farm")}),
              "limits": ["an end-to-end run cannot see a defect the model compensates for", _task_origin(tasks)]}
    for lim in sorted({x for r in rows for x in r.get("limits", [])}):
        result["limits"].append(f"{host.name}: {lim}")
    if any(r.get("skill_load") != "reported" for r in rows):
        result["limits"].append(f"{host.name} does not report which skills loaded; a treatment run counts "
                                "whether or not the skill reached the model ({skill: name} counts reads of "
                                "its SKILL.md instead)")
    if "link" in result["farm"]:
        result["limits"].append("runs used a symlink farm (no copy-on-write clone here): tools that do "
                                "not follow links, such as `find -type f`, saw a different tree")
    path = os.path.join(_dir(ws, aid), f"result-{ts}.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    ledger.append(ws.ledger_path, {"action": "verify", "id": aid, "verdict": v, "result": path,
                                   "host": host.name, "mode": result["mode"], "live_reason": live_reason,
                                   "runs_per_arm": n, "cost_usd": result["cost_usd"]})
    if not keep:
        shutil.rmtree(base, ignore_errors=True)
    result["path"] = path
    return result


def _task_origin(tasks: dict) -> str:
    seen = sum(bool(t.get("from_evidence")) for t in tasks["tasks"])
    if not seen:
        return "tasks and their answers were written by a person, not drawn from the evidence"
    return (f"{seen} of {len(tasks['tasks'])} tasks were drafted from recorded sessions and reviewed by a "
            "person; their expected answers came from the live system on the day those sessions ran")


# ------------------------------------------------------------------------ check
def check(art: dict, eval_ref: str) -> list:
    """Problems with using `eval_ref` as evidence for applying `art`. Empty list: usable.
    Called by the promoter; it never raises on a bad file, it reports it."""
    try:
        with open(eval_ref) as f:
            r = json.load(f)
    except (OSError, ValueError) as e:
        return [f"{eval_ref} is not a readable verify result ({type(e).__name__})"]
    if r.get("schema") != SCHEMA or "verdict" not in r:
        return [f"{eval_ref} is not a `runtune verify` result"]
    problems = []
    if r.get("artifact") != art["id"]:
        problems.append(f"it verified {r.get('artifact')}, not {art['id']}")
    if r.get("proposal_digest") != proposal_digest(art):
        problems.append("the draft changed after it was verified; verify again")
    if r.get("base_digest") != file_digest(art["target"]):
        problems.append(f"{art['target']} changed after it was verified; verify again")
    if r.get("verdict") != "pass":
        problems.append(f"the verdict is `{r.get('verdict')}`, not `pass`: " + "; ".join(r.get("reasons", [])[:2]))
    return problems
