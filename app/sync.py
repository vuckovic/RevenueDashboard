"""One sync pass: pull from each enabled source -> Postgres -> Excel Online."""
import os
import traceback

import db
import excel
from connectors import ubw, workday

SOURCES = {"workday": workday.fetch, "ubw": ubw.fetch}


def enabled_sources():
    out = []
    if os.getenv("WORKDAY_REPORT_URL"):
        out.append("workday")
    if os.getenv("UBW_ENABLED", "false").lower() == "true":
        out.append("ubw")
    return out


def run_source(name):
    run_id = db.start_run(name)
    try:
        rows = SOURCES[name]()
        db.upsert_invoices(name, rows)
        db.finish_run(run_id, "ok", len(rows))
        return len(rows)
    except Exception as e:  # log and continue
        db.finish_run(run_id, "error", message=f"{e}\n{traceback.format_exc()}")
        raise


def run_excel():
    if os.getenv("EXCEL_ENABLED", "false").lower() != "true":
        return
    run_id = db.start_run("excel")
    try:
        n = excel.publish(db.all_invoices())
        db.finish_run(run_id, "ok", n)
    except Exception as e:
        db.finish_run(run_id, "error", message=str(e))
        raise


def run_all():
    for s in enabled_sources():
        try:
            run_source(s)
        except Exception:
            pass
    try:
        run_excel()
    except Exception:
        pass
