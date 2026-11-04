"""PostgreSQL access: invoices (paid rows as JSONB), sync_runs (log),
wdar (Workday report) and ubwar (UBW report) — one TEXT column per report field,
kept up to date incrementally with a change log (report_changes)."""
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
-- Report tables, incremental: one row per row_key, updated in place each sync
-- (see upsert_report). Report fields are added as TEXT columns automatically.
--   wdar  = Workday report "DV_Customer Invoices"
--   ubwar = UBW browser template UBW_TEMPLATE_ID
CREATE TABLE IF NOT EXISTS wdar  (id SERIAL PRIMARY KEY, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ubwar (id SERIAL PRIMARY KEY, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE wdar  ADD COLUMN IF NOT EXISTS row_key TEXT, ADD COLUMN IF NOT EXISTS row_hash TEXT,
                  ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS row_key TEXT, ADD COLUMN IF NOT EXISTS row_hash TEXT,
                  ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
-- Change log for both sources: what changed between syncs.
CREATE TABLE IF NOT EXISTS report_changes (
    id             SERIAL PRIMARY KEY,
    source         TEXT NOT NULL,      -- workday | ubw
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_key        TEXT NOT NULL,
    change         TEXT NOT NULL,      -- new | changed | removed (workday) / closed (ubw) | reopened
    invoice_number TEXT,
    customer_id    TEXT,
    old_status     TEXT,
    new_status     TEXT,
    old_amount_due TEXT,
    new_amount_due TEXT
);
-- One-time move of the earlier UBW-only change log into report_changes.
DO $$ BEGIN
  IF to_regclass('ubwar_changes') IS NOT NULL THEN
    INSERT INTO report_changes (source, changed_at, row_key, change, invoice_number, customer_id,
                                old_status, new_status, old_amount_due, new_amount_due)
    SELECT 'ubw', changed_at, row_key, change, invoice_number, customer_id,
           old_status, new_status, old_rest_curr, new_rest_curr FROM ubwar_changes ORDER BY id;
    DROP TABLE ubwar_changes;
  END IF;
END $$;
"""

# Per report table: source name, unique key fields, fields ignored for change detection,
# what a row dropping out of the report means, and which columns go into the change log
# (invoice number, customer, status, amount due).
REPORTS = {
    "wdar": dict(source="workday",
                 keys=lambda: os.getenv("WORKDAY_KEY_FIELDS") or os.getenv("WORKDAY_ID_FIELD") or "invoice_number",
                 ignore=lambda: os.getenv("WORKDAY_HASH_IGNORE", ""),
                 gone="removed",
                 log=("invoice_number", "customer_id", "payment_status", "amount_due_lc")),
    "ubwar": dict(source="ubw",
                  keys=lambda: os.getenv("UBW_KEY_FIELDS", "client,voucher_type,voucher_no,sequence_no"),
                  ignore=lambda: os.getenv("UBW_HASH_IGNORE", "f0_dso"),
                  gone="closed",
                  log=("ext_inv_ref", "apar_id", "status", "rest_curr")),
}


def _col(name):
    """Report field name -> safe snake_case column name."""
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


def _text(v):
    if v is None:
        return None
    return json.dumps(v) if isinstance(v, (dict, list)) else str(v)


def _split(s):
    return [_col(k.strip()) for k in s.split(",") if k.strip()]


def upsert_report(table, rows):
    """Incremental load of a report table (wdar / ubwar) without duplicates.

    Each row gets a row_key (key fields) and a row_hash (all fields except the ignored
    ones and internal _fields). Then, in one transaction:
      1. log new / changed / reopened rows to report_changes (vs. what is stored),
      2. rows no longer in the report get closed_at (logged as removed/closed),
      3. upsert by row_key (unique index) -> never a duplicate.
    """
    cfg = REPORTS[table]
    src = cfg["source"]
    if not rows:
        raise RuntimeError(f"{src}: report returned 0 rows - nothing updated "
                           "(protects against marking everything removed)")
    cols = _columns(rows)
    key_cols, ignore = _split(cfg["keys"]()), set(_split(cfg["ignore"]()))
    internal = {c for k, c in cols.items() if k.startswith("_")}
    missing = [k for k in key_cols if k not in cols.values()]
    if missing:
        raise RuntimeError(f"{src}: key fields not in report: {missing}")

    staged = {}
    for r in rows:
        d = {cols[k]: _text(v) for k, v in r.items()}
        key = "|".join(d.get(k) or "" for k in key_cols)
        if not key.strip("|"):
            raise RuntimeError(f"{src}: row with empty key {key_cols}: {d}")
        h = json.dumps({k: v for k, v in d.items() if k not in ignore and k not in internal},
                       sort_keys=True)
        staged[key] = (hashlib.sha1(h.encode()).hexdigest(), d)
    duplicates = len(rows) - len(staged)

    names = ", ".join(f'"{c}"' for c in cols.values())
    exprs = ", ".join(f"data->>'{c}'" for c in cols.values())
    sets = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols.values())
    inv, cust, status, due = cfg["log"]
    o, n = "to_jsonb(u)", "s.data"
    stage = f"{table}_stage"

    with connect() as c, c.transaction():
        c.execute(SCHEMA)
        for col in cols.values():
            c.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" TEXT')
        c.execute(f"DELETE FROM {table} WHERE row_key IS NULL")  # rows from the old full-refresh loads
        c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_row_key ON {table} (row_key)")
        first_load = not c.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()[0]

        c.execute(f"CREATE TEMP TABLE {stage} (row_key TEXT PRIMARY KEY, row_hash TEXT, data JSONB) "
                  "ON COMMIT DROP")
        with c.cursor() as cur:
            cur.executemany(f"INSERT INTO {stage} VALUES (%s, %s, %s)",
                            [(k, h, json.dumps(d)) for k, (h, d) in staged.items()])

        # 1) new / changed / reopened (not logged on the very first load)
        if not first_load:
            c.execute(f"""
                INSERT INTO report_changes (source, row_key, change, invoice_number, customer_id,
                                            old_status, new_status, old_amount_due, new_amount_due)
                SELECT %s, s.row_key,
                       CASE WHEN u.row_key IS NULL THEN 'new'
                            WHEN u.closed_at IS NOT NULL THEN 'reopened' ELSE 'changed' END,
                       {n}->>'{inv}', {n}->>'{cust}', {o}->>'{status}', {n}->>'{status}',
                       {o}->>'{due}', {n}->>'{due}'
                FROM {stage} s LEFT JOIN {table} u USING (row_key)
                WHERE u.row_key IS NULL OR u.closed_at IS NOT NULL
                   OR u.row_hash IS DISTINCT FROM s.row_hash""", (src,))
        # 2) stored as current, but no longer in the report
        gone = f"u.closed_at IS NULL AND NOT EXISTS (SELECT 1 FROM {stage} s WHERE s.row_key = u.row_key)"
        c.execute(f"""
            INSERT INTO report_changes (source, row_key, change, invoice_number, customer_id,
                                        old_status, old_amount_due)
            SELECT %s, u.row_key, %s, {o}->>'{inv}', {o}->>'{cust}', {o}->>'{status}', {o}->>'{due}'
            FROM {table} u WHERE {gone}""", (src, cfg["gone"]))
        c.execute(f"UPDATE {table} u SET closed_at = now() WHERE {gone}")
        # 3) upsert: one row per row_key
        c.execute(f"""
            INSERT INTO {table} (row_key, row_hash, first_seen, loaded_at, closed_at, {names})
            SELECT row_key, row_hash, now(), now(), NULL, {exprs} FROM {stage}
            ON CONFLICT (row_key) DO UPDATE SET
                row_hash = EXCLUDED.row_hash, loaded_at = now(), closed_at = NULL, {sets}""")

        stats = dict(c.execute("SELECT change, count(*) FROM report_changes "
                               "WHERE source = %s AND changed_at = now() GROUP BY change", (src,)).fetchall())
        _views(c, table)
    return {"rows": len(staged), "duplicates": duplicates, "first_load": first_load,
            **{k: stats.get(k, 0) for k in ("new", "changed", cfg["gone"], "reopened")}}


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
# Only rows currently in the Workday report (history stays in wdar / report_changes).
WDAR_VIEW = "CREATE OR REPLACE VIEW wdar_v AS SELECT " + ", ".join([
    "id", "loaded_at", "company", "customer_id", "customer_name", "customer_contracts_group",
    "invoice_number", "referenceid", "po_number", "memo", "currency",
    "invoice_status", "payment_status", "payment_terms",
    _date("invoice_date"), _date("due_date"),
    _num("invoice_amount"), _num("inv_amount_usd"), _num("amount_due_lc"), _num("amount_due_usd"),
]) + " FROM wdar WHERE closed_at IS NULL"  # rows Workday no longer returns are kept but hidden

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
