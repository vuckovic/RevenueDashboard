"""PostgreSQL access: invoices (paid rows as JSONB), sync_runs (log),
wdar (Workday report) and ubwar (UBW report) — one TEXT column per report field,
replaced atomically each sync, with a change log (report_changes)."""
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
-- Report tables: exact copy of the latest report, replaced atomically each sync
-- (see refresh_report). Report fields are added as TEXT columns automatically.
-- first_seen = when that row_key first appeared (carried over between syncs).
--   wdar  = Workday report "DV_Customer Invoices"
--   ubwar = UBW browser template UBW_TEMPLATE_ID
CREATE TABLE IF NOT EXISTS wdar  (id SERIAL PRIMARY KEY, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ubwar (id SERIAL PRIMARY KEY, loaded_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE wdar  ADD COLUMN IF NOT EXISTS row_key TEXT, ADD COLUMN IF NOT EXISTS row_hash TEXT,
                  ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;
ALTER TABLE ubwar ADD COLUMN IF NOT EXISTS row_key TEXT, ADD COLUMN IF NOT EXISTS row_hash TEXT,
                  ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;
-- Change log for both sources: what changed between syncs.
CREATE TABLE IF NOT EXISTS report_changes (
    id             SERIAL PRIMARY KEY,
    source         TEXT NOT NULL,      -- workday | ubw
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_key        TEXT NOT NULL,
    change         TEXT NOT NULL,      -- new | changed | removed (workday) / closed (ubw)
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

# Per report table: source name, fields that identify a row (change log matching only),
# fields ignored for change detection,
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


def refresh_report(table, rows):
    """Replace a report table (wdar / ubwar) with the latest report - atomically.

    In ONE transaction (Power BI never sees a half-empty table; on any error the
    previous data stays):
      1. compare the new report with the stored one by row_key / row_hash and log
         new / changed / removed rows to report_changes,
      2. DELETE all rows and INSERT the new report (every row, exactly as UBW/Workday
         returned it - the table always matches the source),
      3. carry first_seen over for rows that were already there.
    row_key is only used to compare; rows sharing a key are all kept (counted as
    'duplicates' in the result, which then means the key fields are not unique).
    """
    cfg = REPORTS[table]
    src = cfg["source"]
    if not rows:
        raise RuntimeError(f"{src}: report returned 0 rows - nothing updated (previous data kept)")
    cols = _columns(rows)
    key_cols, ignore = _split(cfg["keys"]()), set(_split(cfg["ignore"]()))
    internal = {c for k, c in cols.items() if k.startswith("_")}
    missing = [k for k in key_cols if k not in cols.values()]
    if missing:
        raise RuntimeError(f"{src}: key fields not in report: {missing}")

    staged = []
    for r in rows:
        d = {cols[k]: _text(v) for k, v in r.items()}
        key = "|".join(d.get(k) or "" for k in key_cols)
        h = json.dumps({k: v for k, v in d.items() if k not in ignore and k not in internal},
                       sort_keys=True)
        staged.append((key, hashlib.sha1(h.encode()).hexdigest(), json.dumps(d)))
    duplicates = len(staged) - len({k for k, _, _ in staged})

    names = ", ".join(f'"{c}"' for c in cols.values())
    exprs = ", ".join(f"s.data->>'{c}'" for c in cols.values())
    inv, cust, status, due = cfg["log"]
    stage, prev = f"{table}_stage", f"{table}_prev"
    log = ("INSERT INTO report_changes (source, row_key, change, invoice_number, customer_id, "
           "old_status, new_status, old_amount_due, new_amount_due) ")

    with connect() as c, c.transaction():
        c.execute(SCHEMA)
        # views depend on the table; drop them, they are recreated at the end
        c.execute(f"DROP VIEW IF EXISTS ar_invoices_v, {table}_v")
        c.execute(f"DROP INDEX IF EXISTS {table}_row_key")  # older versions: unique key
        c.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS closed_at")
        c.execute(f"CREATE INDEX IF NOT EXISTS {table}_row_key_ix ON {table} (row_key)")
        for col in cols.values():
            c.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" TEXT')

        c.execute(f"CREATE TEMP TABLE {stage} (row_key TEXT, row_hash TEXT, data JSONB) ON COMMIT DROP")
        with c.cursor() as cur:
            cur.executemany(f"INSERT INTO {stage} VALUES (%s, %s, %s)", staged)
        # previous state, one row per key
        c.execute(f"""CREATE TEMP TABLE {prev} ON COMMIT DROP AS
                      SELECT DISTINCT ON (row_key) row_key, row_hash, first_seen, to_jsonb(t) AS data
                      FROM {table} t WHERE row_key IS NOT NULL ORDER BY row_key, id""")
        first_load = not c.execute(f"SELECT EXISTS (SELECT 1 FROM {prev})").fetchone()[0]

        if not first_load:
            newk = f"(SELECT DISTINCT ON (row_key) * FROM {stage} ORDER BY row_key)"
            # new + changed
            c.execute(log + f"""
                SELECT %s, s.row_key, CASE WHEN p.row_key IS NULL THEN 'new' ELSE 'changed' END,
                       s.data->>'{inv}', s.data->>'{cust}', p.data->>'{status}', s.data->>'{status}',
                       p.data->>'{due}', s.data->>'{due}'
                FROM {newk} s LEFT JOIN {prev} p USING (row_key)
                WHERE p.row_key IS NULL OR p.row_hash IS DISTINCT FROM s.row_hash""", (src,))
            # removed (workday) / closed (ubw): stored before, not in the report any more
            c.execute(log + f"""
                SELECT %s, p.row_key, %s, p.data->>'{inv}', p.data->>'{cust}', p.data->>'{status}', NULL,
                       p.data->>'{due}', NULL
                FROM {prev} p WHERE NOT EXISTS (SELECT 1 FROM {stage} s WHERE s.row_key = p.row_key)""",
                      (src, cfg["gone"]))

        # replace: the table is now exactly the latest report
        c.execute(f"DELETE FROM {table}")
        c.execute(f"""
            INSERT INTO {table} (row_key, row_hash, first_seen, loaded_at, {names})
            SELECT s.row_key, s.row_hash, coalesce(p.first_seen, now()), now(), {exprs}
            FROM {stage} s LEFT JOIN {prev} p USING (row_key)""")

        stats = dict(c.execute("SELECT change, count(*) FROM report_changes "
                               "WHERE source = %s AND changed_at = now() GROUP BY change", (src,)).fetchall())
        _views(c)
    return {"rows": len(staged), "duplicates": duplicates, "first_load": first_load,
            **{k: stats.get(k, 0) for k in ("new", "changed", cfg["gone"])}}


def _views(c):
    """(Re)create the typed views from the columns that exist; a failure is logged only."""
    for name, build in (("wdar_v", _wdar_view), ("ubwar_v", _ubwar_view)):
        have = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (name[:-2],))}
        if "row_key" not in have:
            continue
        try:
            with c.transaction():
                c.execute(f"DROP VIEW IF EXISTS ar_invoices_v, {name}")
                c.execute(f"CREATE VIEW {name} AS SELECT " + build(have) + f" FROM {name[:-2]}")
        except Exception as e:
            print(f"view {name} not created: {e}", flush=True)
    if c.execute("SELECT to_regclass('wdar_v') IS NOT NULL AND to_regclass('ubwar_v') IS NOT NULL").fetchone()[0]:
        c.execute("CREATE OR REPLACE VIEW ar_invoices_v AS SELECT 'workday' AS source, * FROM wdar_v "
                  "UNION ALL SELECT 'ubw' AS source, * FROM ubwar_v")


_NUM_RE = "'^-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$'"


def _num(c):
    return f"CASE WHEN replace({c}, ',', '') ~ {_NUM_RE} THEN replace({c}, ',', '')::numeric(18,2) END"


def _date(c):
    return (f"CASE WHEN {c} ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN left({c}, 10)::date "
            f"WHEN {c} ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({c}, 'MM/DD/YYYY') END")


def _select(have, spec):
    """spec: [(alias, source column, kind)] -> select list; missing columns become typed NULLs."""
    out = []
    for alias, col, kind in spec:
        if col not in have:
            out.append(f"NULL::{ {'num': 'numeric(18,2)', 'date': 'date'}.get(kind, 'text') } AS {alias}")
        elif kind == "num":
            out.append(f"{_num(col)} AS {alias}")
        elif kind == "date":
            out.append(f"{_date(col)} AS {alias}")
        else:
            out.append(f"{col} AS {alias}")
    return ", ".join(out)


# Power BI columns (same for both sources): (name, kind)
_AR_COLS = [("company", "text"), ("customer_id", "text"), ("customer_name", "text"),
            ("customer_contracts_group", "text"), ("invoice_number", "text"), ("referenceid", "text"),
            ("po_number", "text"), ("memo", "text"), ("currency", "text"), ("invoice_status", "text"),
            ("payment_status", "text"), ("payment_terms", "text"), ("invoice_date", "date"),
            ("due_date", "date"), ("invoice_amount", "num"), ("inv_amount_usd", "num"),
            ("amount_due_lc", "num"), ("amount_due_usd", "num")]


def _wdar_view(have):
    return "id, loaded_at, first_seen, " + _select(have, [(n, n, k) for n, k in _AR_COLS])


# UBW column for each Power BI column (None = UBW has no equivalent).
_UBW_MAP = {"company": "client", "customer_id": "apar_id", "invoice_number": "ext_inv_ref",
            "referenceid": "voucher_no", "memo": "description", "currency": "currency",
            "invoice_status": "voucher_type", "invoice_date": "voucher_date", "due_date": "due_date",
            "invoice_amount": "cur_amount", "amount_due_lc": "rest_curr"}


def _ubwar_view(have):
    spec = [(n, _UBW_MAP.get(n, "-"), k) for n, k in _AR_COLS]
    sel = _select(have, spec)
    if {"rest_curr", "cur_amount"} <= have:  # derived from the open amount
        sel = sel.replace("NULL::text AS payment_status",
                          f"CASE WHEN {_num('rest_curr')} = 0 THEN 'Paid' "
                          f"WHEN {_num('rest_curr')} = {_num('cur_amount')} THEN 'Unpaid' "
                          "ELSE 'Partially Paid' END AS payment_status")
    return "id, loaded_at, first_seen, " + sel


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


def open_items():
    """Open AR rows (amount due > 0) from wdar_v and ubwar_v, whichever exist."""
    cols = ("invoice_number, referenceid, customer_id, customer_name, currency, payment_status, "
            "due_date, amount_due_lc, amount_due_usd")
    with connect() as c:
        parts = [f"SELECT '{src}' AS source, {cols} FROM {view} WHERE amount_due_lc > 0"
                 for src, view in (("workday", "wdar_v"), ("ubw", "ubwar_v"))
                 if c.execute("SELECT to_regclass(%s) IS NOT NULL", (view,)).fetchone()[0]]
        if not parts:
            return []
        cur = c.execute(" UNION ALL ".join(parts) + " ORDER BY due_date NULLS LAST")
        names = [d.name for d in cur.description]
        return [dict(zip(names, r)) for r in cur]


def counts():
    with connect() as c:
        return dict(c.execute("SELECT source, count(*) FROM invoices GROUP BY source").fetchall())
