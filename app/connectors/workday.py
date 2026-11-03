"""Workday RaaS connector for report 'DV_Customer Invoices' (ISU_AR_Collector_DV)."""
import hashlib
import json
import os
import re

import requests


def fetch_report():
    """Return the full report as a list of dicts (Report_Entry)."""
    url = os.environ["WORKDAY_REPORT_URL"]
    if "/ccx/service/customreport2/" not in url:
        raise ValueError(
            "WORKDAY_REPORT_URL must be the RaaS web-service URL "
            "(…/ccx/service/customreport2/<tenant>/<owner>/DV_Customer_Invoices), "
            "not a browser link like …/d/gateway.htmld. In Workday open the report → "
            "Actions → Web Service → View URLs, and copy the JSON link.")
    resp = requests.get(
        url,
        params={"format": "json"},
        auth=(os.environ["WORKDAY_USER"], os.environ["WORKDAY_PASSWORD"]),
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json().get("Report_Entry", [])


def _norm(s):
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _field(rows, name):
    """Match a configured field name to the report's key, ignoring case/spaces/underscores."""
    if not name:
        return None
    keys = {k for r in rows for k in r}
    return next((k for k in keys if _norm(k) == _norm(name)), name)


def paid_invoices(rows):
    """Return list of (key, row_dict) for paid invoices."""
    paid_field = _field(rows, os.getenv("WORKDAY_PAID_FIELD"))
    paid_value = os.getenv("WORKDAY_PAID_VALUE", "Paid")
    if paid_field:
        rows = [r for r in rows
                if str(r.get(paid_field, "")).strip().lower() == paid_value.strip().lower()]

    id_field = _field(rows, os.getenv("WORKDAY_ID_FIELD"))
    out = []
    for r in rows:
        key = str(r.get(id_field)) if id_field and r.get(id_field) else \
            hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()
        out.append((key, r))
    return out


def fetch():
    return paid_invoices(fetch_report())
