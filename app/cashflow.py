"""Cash-flow forecast: open AR items (amount due > 0) with expected payment date = due date,
per company, in EUR. EUR values come from the database (amount_due_eur, converted on every
sync with ECB rates - see fx.py), so the dashboard and Power BI show the same numbers.

  CASHFLOW_COMPANIES=workday=Optiva,ubw=Qvantel   source -> company shown on the dashboard
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import fx


def _pairs(s):
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def companies():
    return {k.lower(): v for k, v in
            _pairs(os.getenv("CASHFLOW_COMPANIES", "workday=Optiva,ubw=Qvantel")).items()}


def forecast():
    comp = companies()
    items, skipped = [], {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in db.open_items():
        if r["due_date"] is None:
            skip("no due date")
            continue
        cur = (r["currency"] or "").strip().upper()
        if r["amount_due_eur"] is None:
            skip(f"no FX rate for {cur or 'blank currency'}")
            continue
        eur = float(r["amount_due_eur"])
        items.append({
            "company": comp.get(r["source"], r["source"]),
            "invoice": r["invoice_number"] or r["referenceid"],
            "customer": r["customer_name"] or r["customer_id"],
            "currency": cur,
            "amount": float(r["amount_due_lc"]),
            "amount_eur": round(eur, 2),
            "due_date": r["due_date"].isoformat(),
            "status": r["payment_status"],
        })

    today = datetime.now(ZoneInfo(os.getenv("SYNC_TZ", "Europe/Belgrade"))).date()
    order = list(dict.fromkeys(comp.values()))
    order += sorted({i["company"] for i in items} - set(order))
    return {"currency": "EUR", "today": today.isoformat(), "companies": order,
            "fx": fx.rates(), "skipped": skipped, "items": items}
