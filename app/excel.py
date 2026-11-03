"""Publish invoices to an Excel Online workbook (SharePoint/OneDrive) via Microsoft Graph.

Simplest approach: build the .xlsx in memory and overwrite the file. Power BI
connects to that file as its data source.
"""
import io
import os

import msal
import requests
from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo

GRAPH = "https://graph.microsoft.com/v1.0"


def _token():
    app = msal.ConfidentialClientApplication(
        os.environ["MS_CLIENT_ID"],
        authority=f"https://login.microsoftonline.com/{os.environ['MS_TENANT_ID']}",
        client_credential=os.environ["MS_CLIENT_SECRET"],
    )
    res = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in res:
        raise RuntimeError(f"Graph auth failed: {res.get('error_description')}")
    return res["access_token"]


def build_xlsx(invoices):
    cols = []
    for inv in invoices:
        for k in inv:
            if k not in cols:
                cols.append(k)
    wb = Workbook()
    ws = wb.active
    ws.title = "PaidInvoices"
    ws.append(cols or ["source"])
    for inv in invoices:
        ws.append([_cell(inv.get(c)) for c in cols])
    if invoices:
        ref = f"A1:{ws.cell(row=1, column=len(cols)).column_letter}{len(invoices) + 1}"
        t = Table(displayName="PaidInvoices", ref=ref)
        t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
        ws.add_table(t)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _cell(v):
    if isinstance(v, (dict, list)):
        return str(v)
    return v


def publish(invoices):
    data = build_xlsx(invoices)
    url = f"{GRAPH}/drives/{os.environ['MS_DRIVE_ID']}/root:/{os.environ['MS_FILE_PATH']}:/content"
    r = requests.put(url, data=data, headers={
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }, timeout=120)
    r.raise_for_status()
    return len(invoices)
