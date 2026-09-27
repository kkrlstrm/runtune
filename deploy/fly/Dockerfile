# runtune on fly.io — one small machine that runs the loop on a schedule:
#   weekly   derive + review, then ONE digest to Slack/email (card PNG + numbered list)
#   hourly   read the approver's reply; apply approved artifacts into a clone of the
#            target repo, commit on a branch, open a pull request (the merge is the apply)
#   daily    snapshot the OpenRouter bill (the provider keeps 30 days)
#
# Pure Python. Chromium is here only to render the card; without it the digest is text.
# Build context: the runtune repo root.  fly deploy --ha=false --config deploy/fly/fly.toml
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC \
    RUNTUNE_HOME=/data/runtune PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN set -eux; apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git tzdata fonts-inter fonts-dejavu-core; \
    curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic; chmod +x /usr/local/bin/supercronic; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE /app/
COPY runtune/ /app/runtune/
RUN pip install --no-cache-dir '.[postgres]' playwright \
 && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY deploy/fly/crontab /app/crontab
COPY deploy/fly/*.sh /app/deploy/fly/
RUN chmod +x /app/deploy/fly/*.sh
ENTRYPOINT ["/app/deploy/fly/entrypoint.sh"]
