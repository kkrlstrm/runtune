---
name: use-ttt-cli
description: Use when you need `ttt` from the shell — call its CLI, don't write python3 -c
---

# `ttt` from the shell

Agents wrote 5,899 inline programs across 406 sessions / 17 weeks just to import `ttt` (mean 574 chars).
Call the CLI instead. These invocations have succeeded in recorded runs:

- `scripts/ttt db master …`
- `scripts/ttt db query …`
- `scripts/ttt web fetch …`
- `scripts/ttt clickup tasks …`
- `scripts/ttt gitlab read …`
- `scripts/ttt gitlab tree …`
- `scripts/ttt clickup queue …`
- `scripts/ttt clickup task …`
- `scripts/ttt client …`
- `scripts/ttt web search …`

Functions agents reached for most (what the CLI must cover):

- `web.fetch()` — 1412 snippets
- `db.query()` — 1315 snippets
- `web.search()` — 1245 snippets
- `db.query_master()` — 637 snippets
- `clickup.tasks_for()` — 254 snippets
- `clickup.task()` — 180 snippets
- `slack.history()` — 164 snippets
- `db.query_telemetry()` — 148 snippets

If what you need is not covered, import the library — and say so, so it can be added.
