"""The only code in RunTune that writes outside its own workspace.

Derivation proposes; a person decides; this module carries the decision out —
and refuses when the decision outruns its evidence or its authority. The rules
it enforces are the point of the whole system: agents get better from their runs
WITHOUT the learner being able to rewrite its own safety boundaries.

  1. PROPOSE-THEN-APPLY. Nothing is applied without a named approver. There is no
     code path from `derive` to a target file.
  2. DIRECTION. Tightening (a new constraint, a narrower grant, a retired
     clearance) needs an approver. WIDENING — removing or weakening a
     constraint, broadening a grant, admitting a model to the allowlist — needs
     an approver AND a written reason, is logged as a boundary change, and for
     routes needs a passing eval on file.
  3. CEILINGS. A constraint is never armed beyond what its evidence tier allows.
  4. OWNERSHIP. RunTune overwrites only files it created (it marks them); a
     human-written skill or agent is revised in place only through an explicit,
     approved revision with declared lineage — never by name collision.
  5. STALE PARENTS. A proposal reviewed against one version of a target file is
     refused against the next.
  6. PROTECTED PATHS. Host settings, hook wiring and VCS internals are never
     written, whatever the approver says.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone

from ..evidence import tiers
from . import ledger
from .store import Workspace, file_digest

MARK = "runtune-artifact"


class Refused(Exception):
    """A lifecycle action the authority model does not permit. Never swallowed."""


# ---------------------------------------------------------------------- stage
def stage(ws: Workspace, cid: str, target_root: str = ".", revises: str | None = None,
          allow_withheld: bool = False, reason: str | None = None, review_days: int = 28) -> dict:
    cand = ws.find_candidate(cid)
    if not cand:
        raise Refused(f"no candidate {cid} in {ws.root}/candidates")
    if not cand.get("admitted") and not allow_withheld:
        failed = [g["name"] for g in cand.get("gates", []) if not g["passed"] and not g["suppressed"]]
        raise Refused(f"{cid} was withheld by gate(s) {failed}; pass --allow-withheld with --reason to stage anyway")
    if not cand.get("admitted") and not reason:
        raise Refused("staging a withheld candidate requires --reason")
    if ws.get(cid):
        raise Refused(f"{cid} is already staged/active; use --revises to declare a new version")

    target = _target_path(ws, cand, target_root)
    evidence = ws.put_evidence(_evidence_md(cand))
    cases = _cases(cand)
    lineage = []
    if revises:
        prev = ws.get(revises)
        if not prev:
            raise Refused(f"--revises {revises}: no such artifact (lineage is declared, never inferred)")
        if prev["kind"] != cand["kind"]:
            raise Refused("a revision may not change artifact kind; that is a new artifact")
        # Revisions answer to every case their predecessors were built on.
        cases = prev.get("cases", []) + [c for c in cases if c not in prev.get("cases", [])]
        lineage = prev.get("lineage", []) + [revises]

    art = {"id": cid, "kind": cand["kind"], "title": cand["title"], "state": "staged",
           "direction": cand["direction"], "tier": cand["tier"], "candidate": cand,
           "target": target, "base_digest": file_digest(target), "evidence": evidence,
           "cases": cases, "revises": revises, "lineage": lineage,
           "staged_at": _now(), "staged_reason": reason,
           # Epoch stamps (AgentJail/Cedar): what this was validated UNDER. A change to
           # either sends it back for re-validation instead of letting it read as current.
           "policy_digest": ws.policy_digest(),
           "model_epochs": cand.get("numbers", {}).get("model_epochs"),
           "review_after_days": review_days}
    _write_drafts(ws, art)
    ws.put(art)
    ledger.append(ws.ledger_path, {"action": "stage", "id": cid, "kind": cand["kind"],
                                   "direction": cand["direction"], "revises": revises,
                                   "withheld_override": not cand.get("admitted")})
    return art


# ---------------------------------------------------------------------- apply
def apply(ws: Workspace, aid: str, approver: str, action: str | None = None,
          reason: str | None = None, eval_ref: str | None = None) -> dict:
    art = ws.get(aid)
    if not art:
        raise Refused(f"no staged artifact {aid}")
    if art["state"] != "staged":
        raise Refused(f"{aid} is {art['state']}, not staged")
    if not approver or not approver.strip():
        raise Refused("apply needs --approve <name>: the learner never applies its own proposals")
    auth = ws.authority()
    _check_protected(art["target"], auth)
    if file_digest(art["target"]) != art["base_digest"]:
        raise Refused(f"{art['target']} changed since {aid} was staged; re-derive and re-stage "
                      "(a review of one version is not a review of the next)")
    widen = art["direction"] == "widen"
    if widen and not reason:
        raise Refused("this proposal WIDENS what agents may do; applying it needs --reason")
    kind = art["kind"]
    cand = art["candidate"]

    if kind == "constraint":
        rule = dict(cand["proposal"]["ruleset_rule"])
        rule["action"] = action or rule.get("action", "monitor")
        if not tiers.within_ceiling(rule["action"], art["tier"]):
            raise Refused(f"action `{rule['action']}` exceeds the `{art['tier']}` tier's ceiling "
                          f"`{tiers.ceiling(art['tier'])}`")
        rule.setdefault("meta", {})[MARK] = aid
        _upsert_rule(art["target"], rule)
    elif kind == "capability":
        _write_owned(art["target"], "SKILL.md", cand["proposal"]["skill_md"], aid)
    elif kind == "subagent":
        prop = cand["proposal"]
        if prop.get("revises"):
            _revise_agent_grant(art["target"], prop["grant"], aid)
        elif prop.get("agent_md"):
            _write_owned_file(art["target"], prop["agent_md"], aid)
        else:
            raise Refused("lint findings are fixed by hand; there is nothing to apply")
    elif kind == "route":
        _apply_route(art, eval_ref)
    else:
        raise Refused(f"unknown kind {kind}")

    art.update(state="active", applied_at=_now(),
               review_after=(datetime.now(timezone.utc) + timedelta(days=art.get("review_after_days", 28))).isoformat(), approver=approver, applied_reason=reason,
               applied_action=action, eval_ref=eval_ref, applied_digest=file_digest(art["target"]))
    ws.put(art)
    ledger.append(ws.ledger_path, {"action": "apply", "id": aid, "approver": approver,
                                   "boundary_change": widen, "reason": reason, "eval_ref": eval_ref})
    return art


# --------------------------------------------------------------------- retire
def retire(ws: Workspace, aid: str, approver: str, reason: str) -> dict:
    """Retiring a CONSTRAINT loosens a boundary; retiring a capability archives it.
    Both need a named approver and a reason; the first is logged as a boundary change."""
    art = ws.get(aid)
    if not art or art["state"] != "active":
        raise Refused(f"{aid} is not an active artifact")
    if not approver or not reason:
        raise Refused("retire needs --approve and --reason")
    _check_protected(art["target"], ws.authority())
    if art["kind"] == "constraint":
        _remove_rule(art["target"], aid)
    elif art["kind"] in ("capability", "subagent") and os.path.exists(art["target"]):
        if not _owned(art["target"], aid):
            raise Refused(f"{art['target']} is not a RunTune-created file; retire it by hand")
        arch = os.path.join(os.path.dirname(art["target"]), ".archive")
        os.makedirs(arch, exist_ok=True)
        shutil.move(art["target"], os.path.join(arch, os.path.basename(art["target"])))
    art.update(state="retired", retired_at=_now(), retired_by=approver, retired_reason=reason)
    ws.put(art)
    ledger.append(ws.ledger_path, {"action": "retire", "id": aid, "approver": approver, "reason": reason,
                                   "boundary_change": art["kind"] == "constraint"})
    return art


# -------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_path(ws, cand, root) -> str:
    base = os.path.join(root, ws.authority()["targets"][cand["kind"]])
    prop = cand.get("proposal", {})
    if cand["kind"] == "capability":
        name = re.search(r"^name:\s*(.+)$", prop.get("skill_md", ""), re.M)
        return os.path.join(base, (name.group(1).strip() if name else cand["id"]))
    if cand["kind"] == "subagent":
        if prop.get("revises"):
            return os.path.join(base, f"{prop['revises']}.md")
        name = re.search(r"^name:\s*(.+)$", prop.get("agent_md", ""), re.M)
        return os.path.join(base, f"{name.group(1).strip() if name else cand['id']}.md")
    return base


def _check_protected(target: str, auth: dict) -> None:
    norm = os.path.normpath(target)
    for p in auth.get("protected", []):
        p = p.rstrip("/")
        if norm.endswith(os.path.normpath(p)) or f"/{p}/" in f"/{norm}/":
            raise Refused(f"{target} is a protected path ({p}); RunTune never writes it")


def _cases(cand) -> list:
    """What review will re-measure. Stored as selectors, not as copies of events."""
    k, p = cand["kind"], cand.get("proposal", {})
    if k == "constraint" and p.get("ruleset_rule", {}).get("any"):
        return [{"type": "pattern", "surface": "shell", "pattern": p["ruleset_rule"]["any"][0]}]
    if k == "capability" and cand["key"].startswith("inline|"):
        root = cand["key"].split("|", 1)[1]
        return [{"type": "adoption", "root": root}]
    if k == "subagent" and p.get("agent_md"):
        name = re.search(r"^name:\s*(.+)$", p["agent_md"], re.M)
        return [{"type": "agent_type", "name": name.group(1).strip() if name else None}]
    if k == "route":
        return [{"type": "route", "key": cand["key"]}]
    return []


def _evidence_md(cand) -> str:
    lines = [f"# {cand['title']}", "", cand["claim"], "", f"tier: {cand['tier']} · direction: {cand['direction']}",
             "", "## numbers", "```json", json.dumps(cand.get("numbers", {}), indent=2, default=str), "```"]
    for label, key in (("correction (what it targets)", "correction"),
                       ("preservation (what it must not disturb)", "preservation")):
        lines += ["", f"## {label}"]
        for s in cand.get(key, []) or ["(none recorded)"]:
            lines.append(f"- `{json.dumps(s, default=str)[:300]}`")
    lines += ["", "## gates"] + [f"- {'✓' if g['passed'] else ('~' if g['suppressed'] else '✗')} "
                                  f"{g['name']}: {g['reason']}" for g in cand.get("gates", [])]
    lines += ["", "## ladder"] + [f"- {r[0]}: {'closes' if r[1] else 'does not close'} — {r[2]}"
                                   for r in cand.get("ladder", [])]
    return "\n".join(lines) + "\n"


def _write_drafts(ws, art) -> None:
    d = os.path.join(ws.root, "drafts", art["id"])
    os.makedirs(d, exist_ok=True)
    p = art["candidate"].get("proposal", {})
    for key, fn in (("skill_md", "SKILL.md"), ("agent_md", "agent.md")):
        if p.get(key):
            with open(os.path.join(d, fn), "w") as f:
                f.write(p[key])
    with open(os.path.join(d, "proposal.json"), "w") as f:
        json.dump(p, f, indent=2, default=str)


def _load_rules(path):
    if not os.path.exists(path):
        return {"rules": []}, "dict"
    with open(path) as f:
        data = json.load(f)
    return (data, "dict") if isinstance(data, dict) else ({"rules": data}, "list")


def _save_rules(path, data, shape):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data if shape == "dict" else data["rules"], f, indent=2)
        f.write("\n")


def _upsert_rule(path, rule):
    data, shape = _load_rules(path)
    rules = [r for r in data.get("rules", []) if r.get("id") != rule["id"]]
    for r in data.get("rules", []):
        if r.get("id") == rule["id"] and r.get("meta", {}).get(MARK) != rule["meta"][MARK]:
            raise Refused(f"rule id {rule['id']} exists and was not written by this artifact")
    data["rules"] = rules + [rule]
    _save_rules(path, data, shape)


def _remove_rule(path, aid):
    data, shape = _load_rules(path)
    before = len(data.get("rules", []))
    data["rules"] = [r for r in data.get("rules", []) if r.get("meta", {}).get(MARK) != aid]
    if len(data["rules"]) == before:
        raise Refused(f"no rule written by {aid} in {path}")
    _save_rules(path, data, shape)


def _owned(path, aid=None) -> bool:
    side = os.path.join(path, ".runtune.json") if os.path.isdir(path) else None
    if side and os.path.exists(side):
        with open(side) as f:
            return aid is None or json.load(f).get("artifact") == aid
    if os.path.isfile(path):
        with open(path) as f:
            text = f.read()
        return f"<!-- {MARK}:" in text and (aid is None or f"<!-- {MARK}:{aid} -->" in text)
    return False


def _write_owned(skill_dir, fname, content, aid):
    if os.path.exists(skill_dir) and not _owned(skill_dir):
        raise Refused(f"{skill_dir} exists and was not created by RunTune; it will not be overwritten")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, fname), "w") as f:
        f.write(content)
    with open(os.path.join(skill_dir, ".runtune.json"), "w") as f:
        json.dump({"created_by": "runtune", "artifact": aid, "created": _now()}, f, indent=2)


def _write_owned_file(path, content, aid):
    if os.path.exists(path) and not _owned(path):
        raise Refused(f"{path} exists and was not created by RunTune; it will not be overwritten")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content.rstrip("\n") + f"\n\n<!-- {MARK}:{aid} -->\n")


def _revise_agent_grant(path, grant, aid):
    if not os.path.exists(path):
        raise Refused(f"{path} does not exist; nothing to revise")
    with open(path) as f:
        text = f.read()
    m = re.search(r"^tools:\s*(.+)$", text, re.M)
    if not m:
        raise Refused(f"{path} declares no tools line; a grant cannot be narrowed that is not declared")
    old = {t.strip() for t in m.group(1).split(",")}
    if not set(grant) <= old:
        raise Refused("a revision may only REMOVE tools from a grant; widening is a new, reviewed proposal")
    text = text[:m.start()] + "tools: " + ", ".join(grant) + text[m.end():]
    with open(path, "w") as f:
        f.write(text.rstrip("\n") + f"\n<!-- {MARK}:{aid} (grant narrowed from: {', '.join(sorted(old))}) -->\n")


def _apply_route(art, eval_ref):
    prop = art["candidate"].get("proposal", {})
    path = art["target"]
    if prop.get("eval_request"):
        if not eval_ref or not os.path.exists(eval_ref):
            raise Refused("admitting a model to a mode needs --eval <path to a passing eval result>")
        with open(path) as f:
            data = json.load(f)
        req = prop["eval_request"]
        mode = data.setdefault("modes", {}).setdefault(req["mode"], {})
        mode.update(model=req["candidate"], verified_date=date.today().isoformat(),
                    runtune_eval_ref=eval_ref, runtune_previous_model=req["incumbent"])
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return
    if prop.get("retire_mode"):
        with open(path) as f:
            data = json.load(f)
        spec = data.get("modes", {}).pop(prop["retire_mode"], None)
        if spec is None:
            raise Refused(f"mode {prop['retire_mode']} is not in {path}")
        data.setdefault("retired", {})[prop["retire_mode"]] = {**spec, "retired_by": "runtune",
                                                                "retired_on": date.today().isoformat()}
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return
    if prop.get("route_check"):
        checks = os.path.join(os.path.dirname(path) or ".", "route-checks.json")
        data = json.load(open(checks)) if os.path.exists(checks) else {"checks": []}
        data["checks"] = [c for c in data["checks"] if c.get("artifact") != art["id"]]
        data["checks"].append({**prop["route_check"], "artifact": art["id"]})
        with open(checks, "w") as f:
            json.dump(data, f, indent=2)
        return
    raise Refused("this route finding has no mechanical apply; act on it by hand")
