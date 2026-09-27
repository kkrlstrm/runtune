#!/bin/bash
set -euo pipefail
mkdir -p /data/runtune /data/work
for k in RUNTUNE_DB_URL RUNTUNE_SLACK_TOKEN RUNTUNE_SLACK_USER RUNTUNE_APPROVER; do
  [ -n "${!k:-}" ] || echo "runtune: WARNING secret $k is not set" >&2
done
if [ -n "${RUNTUNE_GMAIL_TOKEN_JSON:-}" ]; then
  umask 077; printf '%s' "$RUNTUNE_GMAIL_TOKEN_JSON" > /data/runtune/gmail-token.json
  export RUNTUNE_GMAIL_TOKEN=/data/runtune/gmail-token.json
fi
# The repo approved artifacts are applied to. Cloned once; refreshed before every apply.
if [ -n "${RUNTUNE_GIT_REPO:-}" ] && [ ! -d /data/target/.git ]; then
  git clone "https://x-access-token:${RUNTUNE_GIT_TOKEN}@github.com/${RUNTUNE_GIT_REPO}.git" /data/target
fi
exec supercronic -passthrough-logs /app/crontab
