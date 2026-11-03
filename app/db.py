"""PostgreSQL access: invoices (raw rows as JSONB), sync_runs (log), wdar (Workday report, one column per field)."""
import json
import os
import re

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
-- WDAR: Workday report "DV_Customer Invoices", full refresh each sync.
-- Report fields are added as TEXT columns automatically on load (see replace_wdar).
CREATE TABLE IF NOT EXISTS wdar (
    id          SERIAL PRIMARY KEY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _col(name):
    """Workday field name -> safe snake_case column name."""
    c = re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_") or "field"
    if c[0].isdigit() or c in ("id", "loaded_at"):
        c = "f_" + c
    return c[:63]


def replace_wdar(rows):
    """Replace the contents of WDAR with the report rows (list of dicts)."""
    cols = {}
    for r in rows:
        for k in r:
            if k not in cols:
                c, n = _col(k), 2
                while c in cols.values():
                    c, n = f"{_col(k)[:60]}_{n}", n + 1
                cols[k] = c
    with connect() as c, c.transaction(), c.cursor() as cur:
        cur.execute(SCHEMA)
        for col in cols.values():
            cur.execute(f'ALTER TABLE wdar ADD COLUMN IF NOT EXISTS "{col}" TEXT')
        cur.execute("TRUNCATE wdar RESTART IDENTITY")
        if not rows:
            return 0
        names = ", ".join(f'"{v}"' for v in cols.values())
        marks = ", ".join(["%s"] * len(cols))
        cur.executemany(
            f"INSERT INTO wdar ({names}) VALUES ({marks})",
            [[None if r.get(k) is None else
              (json.dumps(r[k]) if isinstance(r[k], (dict, list)) else str(r[k]))
              for k in cols] for r in rows],
        )
        cur.execute(WDAR_VIEW)
    return len(rows)


def _num(c):
    return (f"CASE WHEN replace({c}, ',', '') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN replace({c}, ',', '')::numeric(18,2) END AS {c}")


def _date(c):
    return (f"CASE WHEN {c} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN left({c}, 10)::date "
            f"WHEN {c} ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({c}, 'MM/DD/YYYY') END AS {c}")


# Typed view for Power BI: amounts -> numeric, dates -> date, everything else as-is.
WDAR_VIEW = "CREATE OR REPLACE VIEW wdar_v AS SELECT " + ", ".join([
    "id", "loaded_at", "company", "customer_id", "customer_name", "customer_contracts_group",
    "invoice_number", "referenceid", "po_number", "memo", "currency",
    "invoice_status", "payment_status", "payment_terms",
    _date("invoice_date"), _date("due_date"),
    _num("invoice_amount"), _num("inv_amount_usd"), _num("amount_due_lc"), _num("amount_due_usd"),
]) + " FROM wdar"


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
