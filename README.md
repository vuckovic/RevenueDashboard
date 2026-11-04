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

### How the tables are kept up to date (both sources, no duplicates)
Each hourly sync pulls the report and compares it with the stored table (`wdar` for Workday, `ubwar` for UBW):
- every row has a unique `row_key` — Workday: `WORKDAY_ID_FIELD` (referenceID); UBW: `UBW_KEY_FIELDS` (client, voucher type, voucher no, sequence no). A unique index makes duplicates impossible;
- a `row_hash` over the row detects changes (`WORKDAY_HASH_IGNORE` / `UBW_HASH_IGNORE` exclude fields that change daily, e.g. UBW DSO);
- changes are logged in **`report_changes`** (`source` = workday / ubw): `new`, `changed` (status, amount due, …), `removed` (Workday: no longer in the report – hidden from `wdar_v`) / `closed` (UBW: no longer an open item – shown as *Closed*, 0 due) and `reopened`;
- then rows are updated in place. A pull with 0 rows is rejected so nothing is wrongly removed.

```
docker compose exec db psql -U postgres -d revenue -c "select * from report_changes order by id desc limit 20;"
```

### Current UBW setup
Template **792** "AR: KPI DSO customer" (customer transactions, client X10). It currently returns **open items only**; for paid items ask a UBW user to save a copy of 792 with *all/historical* transactions, share it with the integration user, and change `UBW_TEMPLATE_ID`. `UBW_CRITERIA` can override template filters per run (e.g. `dim_2=; att_2_id=`).

## Configuration
All settings live in `.env` (see `.env.example`). Sources switch on when configured:
- **Workday:** set `WORKDAY_REPORT_URL` (the RaaS URL from the report's *Actions → Web Service → View URLs* — not the browser `gateway.htmld` link) + password. Optionally `WORKDAY_PAID_FIELD` / `WORKDAY_ID_FIELD` once you know the report's column names.
- **UBW:** SOAP Query Engine (`app/connectors/ubw.py`) runs the browser template `UBW_TEMPLATE_ID`; enabled when `UBW_ENABLED=true` and a template id is set.

## Credentials
Credentials are **never** stored in this repo — only in your local `.env`.
