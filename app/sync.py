"""One sync pass: pull from each enabled source -> Postgres."""
import os
import traceback

import db
from connectors import ubw, workday

SOURCES = {"workday": workday.fetch, "ubw": ubw.fetch}


def enabled_sources():
    out = []
    if os.getenv("WORKDAY_REPORT_URL"):
        out.append("workday")
    if os.getenv("UBW_ENABLED", "false").lower() == "true" and os.getenv("UBW_TEMPLATE_ID"):
        out.append("ubw")
    return out


def _summary(table, st):
    """e.g. 'wdar: 2752 rows | new 3, changed 5, removed 0, reopened 0, duplicate keys 0'"""
    parts = ", ".join(f"{k} {v}" for k, v in st.items() if k not in ("rows", "first_load", "duplicates"))
    msg = (f"{table}: {st['rows']} rows | {parts}, duplicate keys {st['duplicates']}"
           + (" (first load)" if st["first_load"] else ""))
    print(msg, flush=True)
    return msg


def run_source(name):
    run_id = db.start_run(name)
    msg = None
    try:
        if name == "workday":
            report = workday.fetch_report()
            msg = _summary("wdar", db.upsert_report("wdar", report))
            rows = workday.paid_invoices(report)  # paid only -> invoices
        elif name == "ubw":
            report = ubw.fetch_report()
            msg = _summary("ubwar", db.upsert_report("ubwar", report))
            rows = ubw.paid_invoices(report)
        else:
            rows = SOURCES[name]()
        db.upsert_invoices(name, rows)
        db.finish_run(run_id, "ok", len(rows), msg)
        return len(rows)
    except Exception as e:  # log and continue
        db.finish_run(run_id, "error", message=f"{e}\n{traceback.format_exc()}")
        raise


def run_all():
    for s in enabled_sources():
        try:
            run_source(s)
        except Exception:
            pass


if __name__ == "__main__":
    # One-off run, e.g.:  docker compose run --rm worker python sync.py workday
    import sys
    db.init()
    for s in sys.argv[1:] or enabled_sources():
        print(s, run_source(s), "paid rows -> invoices")
