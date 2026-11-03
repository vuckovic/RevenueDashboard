"""Web interface + API."""
import io

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import db
import excel
import sync

app = FastAPI(title="Revenue Dashboard v2")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
    db.init()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "counts": db.counts(),
        "runs": db.recent_runs(),
        "sources": sync.enabled_sources(),
    })


@app.get("/api/invoices")
def invoices(source: str | None = None):
    return db.all_invoices(source)


@app.get("/api/runs")
def runs():
    return db.recent_runs(100)


@app.post("/api/sync/{source}")
def trigger(source: str, bg: BackgroundTasks):
    if source == "all":
        bg.add_task(sync.run_all)
    elif source in sync.SOURCES:
        bg.add_task(sync.run_source, source)
    else:
        raise HTTPException(404, "unknown source")
    return {"queued": source}


@app.post("/sync")
def trigger_form(bg: BackgroundTasks):
    bg.add_task(sync.run_all)
    return RedirectResponse("/", status_code=303)


@app.get("/export.xlsx")
def export():
    data = excel.build_xlsx(db.all_invoices())
    return StreamingResponse(io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=paid_invoices.xlsx"})
