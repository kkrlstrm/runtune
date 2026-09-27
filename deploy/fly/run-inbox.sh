#!/bin/bash
# Hourly. Costs nothing when no digest is awaiting a reply.
set -uo pipefail
cd /data/work
[ -d /data/target/.git ] && git -C /data/target pull --ff-only -q || true
python3 -m runtune inbox --target-root /data/target ${RUNTUNE_GIT_REPO:+--git}
