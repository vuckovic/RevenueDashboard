"""UBW (Unit4 Business World) connector — SOAP Query Engine.

Runs a saved UBW browser template (UBW_TEMPLATE_ID) and returns its rows.

Helpers (run inside the container):
  python -m connectors.ubw about                 # connectivity check (no login)
  python -m connectors.ubw templates [text]      # list templates, find the AR template id
  python -m connectors.ubw columns               # show the columns of UBW_TEMPLATE_ID
"""
import os
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

from connectors import paid_rows

NS = "http://services.agresso.com/QueryEngineService/QueryEngineV201101"


def _url():
    url = os.getenv("UBW_WS_URL", "")
    if not url:
        raise ValueError("Set UBW_WS_URL in .env, e.g. "
                         "https://ubw.unit4cloud.com/<tenant>_webservices/service.svc"
                         "?QueryEngineService/QueryEngineV201101")
    return url


def _call(action, body, auth=True):
    creds = ""
    if auth:
        creds = (f"<q:credentials>"
                 f"<q:Username>{escape(os.environ['UBW_USER'])}</q:Username>"
                 f"<q:Client>{escape(os.environ['UBW_CLIENT'])}</q:Client>"
                 f"<q:Password>{escape(os.environ['UBW_PASSWORD'])}</q:Password>"
                 f"</q:credentials>")
    envelope = (f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:q="{NS}">'
                f"<s:Body><q:{action}>{body}{creds}</q:{action}></s:Body></s:Envelope>")
    resp = requests.post(_url(), data=envelope.encode("utf-8"), timeout=600, headers={
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{NS}/{action}"',
    })
    if resp.status_code >= 300:
        raise RuntimeError(f"UBW {action}: HTTP {resp.status_code}: {resp.text[:1000]}")
    return ET.fromstring(resp.content)


def _find(root, name):
    """First element with this local name (namespace-agnostic)."""
    return next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == name), None)


def _text(root, name):
    e = _find(root, name)
    return e.text if e is not None else None


def about():
    return _text(_call("About", "", auth=False), "AboutResult")


def templates(text=""):
    root = _call("GetTemplateList", "<q:formList></q:formList><q:descrList></q:descrList>")
    out = []
    for e in root.iter():
        if e.tag.rsplit("}", 1)[-1] == "TemplateHeader":
            d = {c.tag.rsplit("}", 1)[-1]: c.text for c in e}
            if text.lower() in " ".join(str(v) for v in d.values()).lower():
                out.append(d)
    return out


def fetch_report():
    """Run UBW_TEMPLATE_ID and return all rows as a list of dicts (column -> text)."""
    tid = int(os.environ["UBW_TEMPLATE_ID"])
    root = _call("GetTemplateResultAsDataSet", f"<q:input><q:TemplateId>{tid}</q:TemplateId></q:input>")
    code, err = _text(root, "ReturnCode"), _text(root, "Status")
    if code not in (None, "0"):
        raise RuntimeError(f"UBW template {tid}: ReturnCode {code}: {err}")
    result = _find(root, "TemplateResult")
    if result is None:
        raise RuntimeError("UBW: no TemplateResult in response")
    # The DataSet is either nested XML or an escaped XML string.
    ds = ET.fromstring(result.text) if (result.text or "").strip().startswith("<") else result
    rows = []

    def walk(e):
        for rec in e:
            if rec.tag.rsplit("}", 1)[-1] == "schema":  # skip the xs:schema part
                continue
            kids = list(rec)
            if kids and all(len(k) == 0 for k in kids):  # a row: all children are fields
                rows.append({k.tag.rsplit("}", 1)[-1]: k.text for k in kids})
            else:
                walk(rec)

    walk(ds)
    return rows


def paid_invoices(rows):
    return paid_rows(rows, "UBW")


def fetch():
    return paid_invoices(fetch_report())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "about"
    print("UBW endpoint:", os.getenv("UBW_WS_URL"), "| client:", os.getenv("UBW_CLIENT"), flush=True)
    if cmd == "about":
        print("About:", about())
    elif cmd == "templates":
        found = templates(" ".join(sys.argv[2:]))
        for t in found:
            print(t)
        print(len(found), "template(s) found")
    elif cmd == "columns":
        rows = fetch_report()
        print(len(rows), "rows; columns:", list(rows[0]) if rows else [])
