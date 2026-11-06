"""Cash-flow forecast: open AR items (amount due > 0) with expected payment date = due date,
per company, converted to EUR with fixed rates from .env.

  CASHFLOW_COMPANIES=workday=Optiva,ubw=Qvantel   source -> company shown on the dashboard
  FX_TO_EUR=USD=0.92,SEK=0.087,GBP=1.17            1 unit of currency = x EUR (EUR is always 1)
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import db


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


def fx_rates():
    rates = {"EUR": 1.0}
    for k, v in _pairs(os.getenv("FX_TO_EUR", "")).items():
        try:
            rates[k.upper()] = float(v)
        except ValueError:
            pass
    return rates


def forecast():
    comp, fx = companies(), fx_rates()
    items, skipped = [], {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for r in db.open_items():
        if r["due_date"] is None:
            skip("no due date")
            continue
        cur = (r["currency"] or "").strip().upper()
        if cur in fx:
            eur = float(r["amount_due_lc"]) * fx[cur]
        elif r["amount_due_usd"] is not None and "USD" in fx:  # Workday also has a USD amount
            eur = float(r["amount_due_usd"]) * fx["USD"]
        else:
            skip(f"no FX rate for {cur or 'blank currency'}")
            continue
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
            "fx": fx, "skipped": skipped, "items": items}
