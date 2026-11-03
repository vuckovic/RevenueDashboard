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
| `db` | PostgreSQL 16 — tables `wdar` (full Workday report), `ubwar` (full UBW report) — one TEXT column per field —, `invoices` (paid rows as JSONB) and `sync_runs` (log) |
| `web` | FastAPI web UI + REST API (`/api/invoices`, `/api/runs`, `POST /api/sync/{workday|ubw|all}`) |
| `worker` | Runs a full sync every `SYNC_INTERVAL_MINUTES` |

## Load the Workday report into WDAR now
```
docker compose run --rm worker python sync.py workday
```
Each sync replaces the contents of `wdar` with the current report; new report fields become new columns automatically.

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

## Configuration
All settings live in `.env` (see `.env.example`). Sources switch on when configured:
- **Workday:** set `WORKDAY_REPORT_URL` (the RaaS URL from the report's *Actions → Web Service → View URLs* — not the browser `gateway.htmld` link) + password. Optionally `WORKDAY_PAID_FIELD` / `WORKDAY_ID_FIELD` once you know the report's column names.
- **UBW:** SOAP Query Engine (`app/connectors/ubw.py`) runs the browser template `UBW_TEMPLATE_ID`; enabled when `UBW_ENABLED=true` and a template id is set.

## Credentials
Credentials are **never** stored in this repo — only in your local `.env`.
