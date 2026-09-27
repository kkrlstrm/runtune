"""Postgres access for the telemetry sources — read-only by construction.

Two traps this module exists to close, both of which have cost real data:

  * libpq environment variables (PGHOST, PGDATABASE, …) silently override parts
    of a DSN, so a query can land on a local socket and fail — or worse, succeed
    against the wrong database. They are cleared for the connection.
  * Neon's `-pooler` endpoint cannot serve psycopg's automatically prepared
    statements, so it tests clean and starts failing after ~5 repeated queries.
    `prepare_threshold=None` disables preparation, and a pooled host is warned on.

Every session is opened READ ONLY. RunTune reads telemetry; it never writes to it.
"""

from __future__ import annotations

import os
import sys

_LIBPQ = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSERVICE", "PGOPTIONS")


def resolve_dsn(dsn_or_env: str | None) -> str:
    if not dsn_or_env:
        dsn_or_env = "RUNTUNE_DB_URL"
    if "://" in dsn_or_env:
        return dsn_or_env
    val = os.environ.get(dsn_or_env)
    if not val:
        raise SystemExit(f"runtune: ${dsn_or_env} is not set (pass --db with a DSN or an env var name)")
    return val


def query(dsn: str, sql: str, params=None) -> list[dict]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise SystemExit("runtune: Postgres sources need `pip install 'runtune[postgres]'`") from exc
    if "-pooler" in dsn:
        print("runtune: warning — pooled endpoint; prepared statements disabled", file=sys.stderr)
    saved = {k: os.environ.pop(k) for k in _LIBPQ if k in os.environ}
    try:
        with psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None,
                             connect_timeout=20) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
    finally:
        os.environ.update(saved)
