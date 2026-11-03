"""PostgreSQL access: two tables — invoices (raw rows as JSONB) and sync_runs (log)."""
import json
import os

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    source      TEXT NOT NULL,
    invoice_key TEXT NOT NULL,
    data        JSONB NOT NULL,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, invoice_key)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    rows        INTEGER,
    message     TEXT
);
"""


def connect():
    return psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASSWORD", ""),
        dbname=os.getenv("PG_DB", "revenue"),
        autocommit=True,
    )


def init():
    with connect() as c:
        c.execute(SCHEMA)


def upsert_invoices(source, rows):
    """rows: list of (key, dict)."""
    with connect() as c, c.cursor() as cur:
        cur.executemany(
            """INSERT INTO invoices (source, invoice_key, data) VALUES (%s, %s, %s)
               ON CONFLICT (source, invoice_key)
               DO UPDATE SET data = EXCLUDED.data, loaded_at = now()""",
            [(source, k, json.dumps(d)) for k, d in rows],
        )


def all_invoices(source=None):
    sql = "SELECT source, data FROM invoices"
    args = ()
    if source:
        sql += " WHERE source = %s"
        args = (source,)
    with connect() as c:
        return [{"source": s, **d} for s, d in c.execute(sql + " ORDER BY loaded_at DESC", args)]


def start_run(source):
    with connect() as c:
        return c.execute("INSERT INTO sync_runs (source) VALUES (%s) RETURNING id", (source,)).fetchone()[0]


def finish_run(run_id, status, rows=None, message=None):
    with connect() as c:
        c.execute(
            "UPDATE sync_runs SET finished_at=now(), status=%s, rows=%s, message=%s WHERE id=%s",
            (status, rows, (message or "")[:2000], run_id),
        )


def recent_runs(limit=20):
    with connect() as c:
        cur = c.execute(
            "SELECT id, source, started_at, finished_at, status, rows, message "
            "FROM sync_runs ORDER BY id DESC LIMIT %s", (limit,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur]


def counts():
    with connect() as c:
        return dict(c.execute("SELECT source, count(*) FROM invoices GROUP BY source").fetchall())
