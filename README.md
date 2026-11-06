# Revenue Dashboard v2

Pulls paid-invoice data from Workday (report **DV_Customer Invoices**, ISU `ISU_AR_Collector_DV`, security group `ISG_AR_Collector`) and from UBW (Unit4, SOAP Query Engine) into PostgreSQL. Power BI reads from PostgreSQL (or from the Excel download).

```
Workday RaaS ─┐
              ├─> worker (Python, scheduled) ─> PostgreSQL ─> Power BI
UBW SOAP ────┘                                   │
                                     web (FastAPI): status, manual sync, Excel download, API
```

## Run it (Docker Desktop)
```
cp .env.example .env
open -e .env
docker compose up -d --build
```
Fill in at least `PG_PASSWORD` (and the Workday values) in `.env` before the last command. `.env` is git-ignored — never commit it.
- Web interface: http://localhost:8000  (API docs: http://localhost:8000/docs)
- Logs: `docker compose logs -f worker`

## Containers
| Service | What it does |
|---|---|
| `db` | PostgreSQL 16 — views for Power BI: `wdar_v`, `ubwar_v` (same columns) and **`ar_invoices_v`** (both, with `source`); tables `wdar` (full Workday report), `ubwar` (full UBW report) — one TEXT column per field —, `invoices` (paid rows as JSONB) and `sync_runs` (log) |
| `web` | FastAPI web UI + REST API (`/api/invoices`, `/api/runs`, `POST /api/sync/{workday|ubw|all}`) |
| `worker` | Syncs every hour on the hour, 08:00–17:00 Europe/Belgrade (`SYNC_HOURS`, `SYNC_TZ`, `SYNC_WEEKDAYS_ONLY`) |

## Load the Workday report into WDAR now
```
docker compose run --rm worker python sync.py workday
```
Each sync updates `wdar` in place (see below); new report fields become new columns automatically.

## Load the UBW report into UBWAR
1. In `.env` set `UBW_USER`, `UBW_CLIENT` (company code), `UBW_PASSWORD`, and check `UBW_WS_URL` (web-services endpoint, not the `Default.aspx` link).
2. Check connectivity and find the AR browser template id:
```
docker compose run --rm worker python -m connectors.ubw about
docker compose run --rm worker python -m connectors.ubw templates invoice
```
3. Set `UBW_TEMPLATE_ID` and `UBW_ENABLED=true`, then:
```
docker compose run --rm worker python -m connectors.ubw columns
docker compose run --rm worker python sync.py ubw
```
4. Optionally set `UBW_PAID_FIELD` / `UBW_ID_FIELD` from the column list so only paid rows go to `invoices`.

### How the tables are refreshed (both sources)
Each hourly sync, in **one transaction**:
1. compares the new report with the stored table and logs differences to **`report_changes`** (`new`, `changed` – status / amount due, `removed` (Workday) / `closed` (UBW));
2. deletes all rows and inserts the new report — `wdar` / `ubwar` always match the source exactly (no duplicates, nothing left over);
3. keeps `first_seen` (when a row first appeared) for rows that were already there; `loaded_at` = time of the last refresh.

Power BI never sees a half-empty table, and if a pull fails or returns 0 rows the previous data stays.
Key fields (`WORKDAY_ID_FIELD`, `UBW_KEY_FIELDS`) are only used to match rows for the change log.

```
docker compose exec db psql -U postgres -d revenue -c "select * from report_changes order by id desc limit 20;"
```

### EUR values and due months (both tables)
Every refresh also fills, in the table itself: `fx_rate_eur`, `amount_eur` (invoice amount in EUR),
`amount_due_eur` (open amount in EUR) and `due_month` (first day of the due date's month).
Rates: ECB daily reference rates, stored in `fx_rates` and refreshed at the start of each sync
(`FX_TO_EUR` in `.env` = fallback for currencies the ECB doesn't publish).
The views add `expected_month` (month the open amount is expected: due month, overdue → current month)
and `is_overdue`. Power BI: sum `amount_due_eur` by `expected_month` for expected income per month.
The cash-flow dashboard uses the same EUR values.

```
docker compose exec db psql -U postgres -d revenue -c "select expected_month, source, sum(amount_due_eur) from ar_invoices_v where expected_month is not null group by 1,2 order by 1,2;"
docker compose exec db psql -U postgres -d revenue -c "select * from fx_rates order by currency;"
```

### Current UBW setup
Template **915** (client X10). Check what a template returns with
`python -m connectors.ubw profile` (row types, amounts, key uniqueness — no customer data).
`UBW_CRITERIA` can override template filters per run (e.g. `dim_2=; att_2_id=`).

## Configuration
All settings live in `.env` (see `.env.example`). Sources switch on when configured:
- **Workday:** set `WORKDAY_REPORT_URL` (the RaaS URL from the report's *Actions → Web Service → View URLs* — not the browser `gateway.htmld` link) + password. Optionally `WORKDAY_PAID_FIELD` / `WORKDAY_ID_FIELD` once you know the report's column names.
- **UBW:** SOAP Query Engine (`app/connectors/ubw.py`) runs the browser template `UBW_TEMPLATE_ID`; enabled when `UBW_ENABLED=true` and a template id is set.

## Credentials
Credentials are **never** stored in this repo — only in your local `.env`.
