# Extending RunTune

Three extension points, each with no changes to the core.

## A notification channel

Any class with three methods, loaded from `.runtune/channels.json`:

```python
class TeamsChannel:
    def __init__(self, webhook_env="TEAMS_WEBHOOK", **opts):   # receives its config entry as kwargs
        self.webhook_env = webhook_env

    def send(self, digest: dict, text: str, png: str | None) -> dict:
        ...                        # deliver; return whatever you need to find replies later
        return {"message_id": "..."}

    def replies(self, delivery: dict) -> list[str]:
        return []                  # new replies from the approver, oldest first ([] if send-only)

    def answer(self, delivery: dict, text: str) -> None:
        ...                        # confirm in the same place
```

```bash
runtune channels add plugin --opt module=mypkg.teams:TeamsChannel --opt webhook_env=TEAMS_WEBHOOK
runtune channels test
```

Store environment-variable names in the config, never secrets. `digest` has `title`, `chip`,
`tiles`, `items` (each with `n`, `kind`, `title`, `meta`) and `footer`. `text` is the ready
Slack-style markdown, and `png` is the rendered card when Playwright is available.

## An evidence source

Anything that can produce the portable JSONL format (see `runtune/sources/jsonl.py`) works
with `--jsonl file.jsonl`, and its records are grouped with the same keys as every built-in
source:

```json
{"type": "event", "source": "myagent", "session": "s1", "ts": "2026-09-01T10:00:00Z",
 "surface": "Bash", "text": "psql -c 'select 1'", "ok": false, "error": "connection refused",
 "actor": "root", "model": "my-model"}
{"type": "invocation", "source": "myagent", "invocation": "a1", "session": "s1",
 "agent_type": "researcher", "tools": {"Bash": 12}, "tokens_out": 3000, "tokens_reread": 90000,
 "token_verified": true}
```

A recorder that writes these lines into `~/.runtune/events/<YYYY-MM-DD>.jsonl` is picked up by
the default `local` source with no flags at all.

## A deriver

A deriver is a function `derive(corpus) -> (proposed, withheld)` that returns `Candidate`
objects (`runtune/derive/candidate.py`). Every candidate needs:

- a `claim` in one sentence;
- a `tier`, meaning how much the evidence is allowed to say;
- `gates`: failed gates move the candidate to `withheld`, with the reason shown;
- a `ladder` (why a narrower artifact kind doesn't work);
- `correction` and `preservation` samples;
- a `direction` (`tighten` / `widen` / `neutral`); the promoter's authority rules key on it.

Wire it into `cmd_derive` and `_run_for_digest` in `runtune/cli.py`, and add a fixture with a
known answer.
