#!/bin/bash
# The OpenRouter activity endpoint keeps 30 days; a missed day is unrecoverable.
set -uo pipefail
[ -n "${OPENROUTER_PROVISIONING_KEY:-}" ] && python3 -m runtune record openrouter || echo "runtune: no OpenRouter key; skipped"
