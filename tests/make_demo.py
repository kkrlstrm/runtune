"""Regenerate runtune/data/demo.jsonl from the test fixtures: python3 -m tests.make_demo"""
import json

from tests import fixtures as fx


def main(path="runtune/data/demo.jsonl"):
    out = []
    r = fx.routing()
    out.append({"type": "policy", **{k: v for k, v in r.route_policy.items() if k != "path"}})
    for c in (fx.failing_psql(), fx.inline_ttt(), r):
        for e in c.events:
            d = {"type": "event", "source": e.source, "session": e.session, "ts": e.ts.isoformat(),
                 "surface": e.surface, "text": e.text, "ok": e.ok, "actor": e.actor, "model": e.model}
            if not e.ok:
                d["error"] = e.error or "failed"
            if e.kind == "model":
                d.update(kind="model", usd=e.usd)
            out.append(d)
    out += [{"type": "spend", **s} for s in r.spend]
    for i in fx.fanouts().invocations:
        out.append({"type": "invocation", "source": i.source, "invocation": i.invocation, "session": i.session,
                    "agent_type": i.agent_type, "started": i.started.isoformat(), "status": i.status,
                    "tools": dict(i.tools), "tokens_out": i.tokens_out, "tokens_reread": i.tokens_reread,
                    "token_verified": i.token_verified})
    with open(path, "w") as f:
        f.write("# synthetic trace for `runtune demo` — every number is invented; see tests/fixtures.py\n")
        for d in out:
            f.write(json.dumps(d) + "\n")
    print(len(out), "records ->", path)


if __name__ == "__main__":
    main()
