"""Workday RaaS connector for report 'DV_Customer Invoices' (ISU_AR_Collector_DV)."""
import os

import requests

from connectors import paid_rows


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


def paid_invoices(rows):
    """Return list of (key, row_dict) for paid invoices."""
    return paid_rows(rows, "WORKDAY")


def fetch():
    return paid_invoices(fetch_report())
