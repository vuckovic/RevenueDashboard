"""PostgreSQL access: invoices (paid rows as JSONB), sync_runs (log),
wdar (Workday report) and ubwar (UBW report) — one TEXT column per report field."""
import hashlib
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
-- UBWAR: UBW browser template (UBW_TEMPLATE_ID), incremental: one row per row_key,
-- updated in place each sync (see upsert_ubwar). Report fields are TEXT columns.
CREATE TABLE IF NOT EXISTS ubwar (
    id          SERIAL PRIMARY KEY,
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS row_key    TEXT;
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS row_hash   TEXT;
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS closed_at  TIMESTAMPTZ;
-- Change log: what changed in UBW between syncs.
CREATE TABLE IF NOT EXISTS ubwar_changes (
    id             SERIAL PRIMARY KEY,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_key        TEXT NOT NULL,
    change         TEXT NOT NULL,      -- new | changed | closed | reopened
    invoice_number TEXT,
    customer_id    TEXT,
    old_status     TEXT,
    new_status     TEXT,
    old_rest_curr  TEXT,
    new_rest_curr  TEXT
);
"""

REPORT_TABLES = ("wdar", "ubwar")


def _col(name):
    """Workday field name -> safe snake_case column name."""
    c = re.sub(r"[^0-9a-z]+", "_", name.lower()).strip("_") or "field"
    if c[0].isdigit() or c in ("id", "loaded_at", "row_key", "row_hash", "first_seen", "closed_at"):
        c = "f_" + c
    return c[:63]


def _columns(rows):
    """Report field name -> unique safe column name."""
    cols = {}
    for r in rows:
        for k in r:
            if k not in cols:
                c, n = _col(k), 2
                while c in cols.values():
                    c, n = f"{_col(k)[:60]}_{n}", n + 1
                cols[k] = c
    return cols


def upsert_ubwar(rows):
    """Incremental UBW load without duplicates.

    Each row gets a row_key (UBW_KEY_FIELDS) and a row_hash (all fields except
    UBW_HASH_IGNORE and internal _fields). Then, in one transaction:
      - log new / changed / reopened rows to ubwar_changes (compared with what is stored),
      - upsert by row_key (unique) -> never a duplicate,
      - rows no longer in the report get closed_at (logged as 'closed').
    Returns {'rows', 'new', 'changed', 'closed', 'reopened', 'duplicates'}.
    """
    if not rows:
        raise RuntimeError("UBW returned 0 rows - nothing updated (protects against marking all closed)")
    key_fields = [k.strip() for k in os.getenv(
        "UBW_KEY_FIELDS", "client,voucher_type,voucher_no,sequence_no").split(",") if k.strip()]
    ignore = {k.strip() for k in os.getenv("UBW_HASH_IGNORE", "f0_dso").split(",") if k.strip()}
    missing = [k for k in key_fields if not any(k in r for r in rows)]
    if missing:
        raise RuntimeError(f"UBW_KEY_FIELDS not in report: {missing}")

    staged = {}
    for r in rows:
        key = "|".join(str(r.get(k) or "") for k in key_fields)
        data = {k: v for k, v in r.items() if not k.startswith("_") and k not in ignore}
        staged[key] = (hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest(), r)
    duplicates = len(rows) - len(staged)

    cols = _columns(rows)
    lit = lambda k: "'" + k.replace("'", "''") + "'"
    names = ", ".join(f'"{c}"' for c in cols.values())
    exprs = ", ".join(f"data->>{lit(k)}" for k in cols)
    sets = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols.values())
    old = "to_jsonb(u)"

    with connect() as c, c.transaction():
        c.execute(SCHEMA)
        for col in cols.values():
            c.execute(f'ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS "{col}" TEXT')
        c.execute("DELETE FROM ubwar WHERE row_key IS NULL")  # rows from the old full-refresh load
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ubwar_row_key ON ubwar (row_key)")
        first_load = not c.execute("SELECT EXISTS (SELECT 1 FROM ubwar)").fetchone()[0]

        c.execute("CREATE TEMP TABLE ubw_stage (row_key TEXT PRIMARY KEY, row_hash TEXT, data JSONB) "
                  "ON COMMIT DROP")
        with c.cursor() as cur:
            cur.executemany("INSERT INTO ubw_stage VALUES (%s, %s, %s)",
                            [(k, h, json.dumps(r)) for k, (h, r) in staged.items()])

        # 1) new / changed / reopened (skip logging on the very first load)
        if not first_load:
            c.execute(f"""
                INSERT INTO ubwar_changes (row_key, change, invoice_number, customer_id,
                                           old_status, new_status, old_rest_curr, new_rest_curr)
                SELECT s.row_key,
                       CASE WHEN u.row_key IS NULL THEN 'new'
                            WHEN u.closed_at IS NOT NULL THEN 'reopened' ELSE 'changed' END,
                       s.data->>'ext_inv_ref', s.data->>'apar_id',
                       {old}->>'status', s.data->>'status', {old}->>'rest_curr', s.data->>'rest_curr'
                FROM ubw_stage s LEFT JOIN ubwar u USING (row_key)
                WHERE u.row_key IS NULL OR u.closed_at IS NOT NULL
                   OR u.row_hash IS DISTINCT FROM s.row_hash""")
        # 2) closed: stored as open, but no longer in the report
        c.execute(f"""
            INSERT INTO ubwar_changes (row_key, change, invoice_number, customer_id,
                                       old_status, old_rest_curr)
            SELECT u.row_key, 'closed', {old}->>'ext_inv_ref', {old}->>'apar_id',
                   {old}->>'status', {old}->>'rest_curr'
            FROM ubwar u
            WHERE u.closed_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM ubw_stage s WHERE s.row_key = u.row_key)""")
        c.execute("""UPDATE ubwar u SET closed_at = now()
                     WHERE u.closed_at IS NULL
                       AND NOT EXISTS (SELECT 1 FROM ubw_stage s WHERE s.row_key = u.row_key)""")
        # 3) upsert: one row per row_key
        c.execute(f"""
            INSERT INTO ubwar (row_key, row_hash, first_seen, loaded_at, closed_at, {names})
            SELECT row_key, row_hash, now(), now(), NULL, {exprs} FROM ubw_stage
            ON CONFLICT (row_key) DO UPDATE SET
                row_hash = EXCLUDED.row_hash, loaded_at = now(), closed_at = NULL, {sets}""")

        stats = dict(c.execute("""SELECT change, count(*) FROM ubwar_changes
                                  WHERE changed_at = now() GROUP BY change""").fetchall())
        _views(c, "ubwar")
    return {"rows": len(staged), "duplicates": duplicates, "first_load": first_load,
            **{k: stats.get(k, 0) for k in ("new", "changed", "closed", "reopened")}}


def replace_wdar(rows):
    return replace_report("wdar", rows)


def replace_report(table, rows):
    """Replace the contents of a report table (wdar/ubwar) with rows (list of dicts)."""
    assert table in REPORT_TABLES
    cols = _columns(rows)
    with connect() as c, c.transaction(), c.cursor() as cur:
        cur.execute(SCHEMA)
        for col in cols.values():
            cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" TEXT')
        cur.execute(f"TRUNCATE {table} RESTART IDENTITY")
        if not rows:
            return 0
        names = ", ".join(f'"{v}"' for v in cols.values())
        marks = ", ".join(["%s"] * len(cols))
        cur.executemany(
            f"INSERT INTO {table} ({names}) VALUES ({marks})",
            [[None if r.get(k) is None else
              (json.dumps(r[k]) if isinstance(r[k], (dict, list)) else str(r[k]))
              for k in cols] for r in rows],
        )
        _views(c, table)
    return len(rows)


def _views(c, table):
    """(Re)create the typed views; a view failure is logged, it doesn't fail the load."""
    sql = {"wdar": WDAR_VIEW, "ubwar": UBWAR_VIEW}[table]
    for name, stmt in ((f"{table}_v", sql), ("ar_invoices_v", AR_VIEW)):
        if name == "ar_invoices_v" and not c.execute(
                "SELECT to_regclass('wdar_v') IS NOT NULL AND to_regclass('ubwar_v') IS NOT NULL"
        ).fetchone()[0]:
            continue
        try:
            with c.transaction():
                c.execute(stmt)
        except Exception as e:
            print(f"view {name} not created: {e}", flush=True)


def _num(c, alias=None):
    return (f"CASE WHEN replace({c}, ',', '') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
            f"THEN replace({c}, ',', '')::numeric(18,2) END AS {alias or c}")


def _date(c, alias=None):
    return (f"CASE WHEN {c} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN left({c}, 10)::date "
            f"WHEN {c} ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({c}, 'MM/DD/YYYY') END "
            f"AS {alias or c}")


# Typed view for Power BI: amounts -> numeric, dates -> date, everything else as-is.
WDAR_VIEW = "CREATE OR REPLACE VIEW wdar_v AS SELECT " + ", ".join([
    "id", "loaded_at", "company", "customer_id", "customer_name", "customer_contracts_group",
    "invoice_number", "referenceid", "po_number", "memo", "currency",
    "invoice_status", "payment_status", "payment_terms",
    _date("invoice_date"), _date("due_date"),
    _num("invoice_amount"), _num("inv_amount_usd"), _num("amount_due_lc"), _num("amount_due_usd"),
]) + " FROM wdar"

# UBW (template 792, customer transactions) in the same shape as wdar_v.
# Fields UBW doesn't have are NULL. Amounts are in invoice currency (cur_amount / rest_curr).
# Rows that dropped out of the (open-items) report are 'Closed' with 0 due.
UBWAR_VIEW = "CREATE OR REPLACE VIEW ubwar_v AS SELECT " + ", ".join([
    "id", "loaded_at", "client AS company", "apar_id AS customer_id",
    "NULL::text AS customer_name", "NULL::text AS customer_contracts_group",
    "ext_inv_ref AS invoice_number", "voucher_no AS referenceid",
    "NULL::text AS po_number", "NULL::text AS memo", "currency",
    "voucher_type AS invoice_status",
    "CASE WHEN closed_at IS NOT NULL THEN 'Closed' "
    "WHEN rest_curr ~ '^-?0+(\\.0+)?$' THEN 'Paid' "
    "WHEN rest_curr = cur_amount THEN 'Unpaid' ELSE 'Partially Paid' END AS payment_status",
    "NULL::text AS payment_terms",
    _date("voucher_date", "invoice_date"), _date("due_date"),
    _num("cur_amount", "invoice_amount"), "NULL::numeric(18,2) AS inv_amount_usd",
    "CASE WHEN closed_at IS NOT NULL THEN 0::numeric(18,2) ELSE "
    + _num("rest_curr", "x").rsplit(" AS ", 1)[0] + " END AS amount_due_lc",
    "NULL::numeric(18,2) AS amount_due_usd",
]) + " FROM ubwar"

# One AR view for Power BI: Workday + UBW, same columns, plus source.
AR_VIEW = ("CREATE OR REPLACE VIEW ar_invoices_v AS "
           "SELECT 'workday' AS source, * FROM wdar_v UNION ALL SELECT 'ubw' AS source, * FROM ubwar_v")


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
