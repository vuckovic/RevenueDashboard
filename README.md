# Revenue Dashboard v2

Pulls paid-invoice data from Workday (report **DV_Customer Invoices**, ISU `ISU_AR_Collector_DV`, security group `ISG_AR_Collector`) — and later UBW — into PostgreSQL, publishes it to an Excel Online workbook, and Power BI reads that workbook.

```
Workday RaaS ─┐
              ├─> worker (Python, scheduled) ─> PostgreSQL ─> Excel Online (Graph) ─> Power BI
UBW (todo) ───┘                                   │
                                     web (FastAPI): status, manual sync, Excel download, API
```

## Run it (Docker Desktop)
```
cp .env.example .env      # fill in passwords locally — never commit .env
docker compose up -d --build
```
- Web interface: http://localhost:8000  (API docs: http://localhost:8000/docs)
- Logs: `docker compose logs -f worker`

## Containers
| Service | What it does |
|---|---|
| `db` | PostgreSQL 16 — tables `invoices` (raw rows as JSONB) and `sync_runs` (log) |
| `web` | FastAPI web UI + REST API (`/api/invoices`, `/api/runs`, `POST /api/sync/{workday|ubw|excel|all}`) |
| `worker` | Runs a full sync every `SYNC_INTERVAL_MINUTES` |

## Configuration
All settings live in `.env` (see `.env.example`). Sources switch on when configured:
- **Workday:** set `WORKDAY_REPORT_URL` + password. Optionally `WORKDAY_PAID_FIELD` / `WORKDAY_ID_FIELD` once you know the report's column names.
- **UBW:** placeholder in `app/connectors/ubw.py` — access method not decided yet.
- **Excel Online:** needs an Entra ID app registration with Graph `Files.ReadWrite.All` (application), then `EXCEL_ENABLED=true`.

## Credentials
Credentials are **never** stored in this repo — only in your local `.env`.
