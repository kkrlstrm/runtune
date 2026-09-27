#!/bin/bash
# Derive + review over the warehouse, then send ONE digest. Nothing is applied here.
set -uo pipefail
cd /data/work
[ -d /data/target/.git ] && git -C /data/target pull --ff-only -q || true
ROUTES=${RUNTUNE_ROUTES:+--routes /data/target/$RUNTUNE_ROUTES}
python3 -m runtune notify ${RUNTUNE_CHANNEL:+--channel $RUNTUNE_CHANNEL} --days "${RUNTUNE_DAYS:-60}" \
  --target-root /data/target $ROUTES ${RUNTUNE_AGENTS_DIR:+--agents-dir /data/target/$RUNTUNE_AGENTS_DIR}
