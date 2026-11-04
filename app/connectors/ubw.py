"""UBW (Unit4 Business World) connector — SOAP Query Engine.

Runs a saved UBW browser template (UBW_TEMPLATE_ID) and returns its rows.

Helpers (run inside the container):
  python -m connectors.ubw about                 # connectivity check (no login)
  python -m connectors.ubw templates [text]      # list templates, find the AR template id
  python -m connectors.ubw criteria              # show the filters of UBW_TEMPLATE_ID
  python -m connectors.ubw columns               # show the columns (+3 sample rows)

Filters can be overridden per run with UBW_CRITERIA, e.g. "status=; period=202401..202612"
(empty value = no restriction).
"""
import os
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

from connectors import paid_rows

NS = "http://services.agresso.com/QueryEngineService/QueryEngineV201101"
last_response = ""


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


def criteria(tid):
    """The template's search criteria: list of SearchCriteriaProperties elements."""
    root = _call("GetSearchCriteria",
                 f"<q:templateId>{tid}</q:templateId><q:hideUnused>false</q:hideUnused>")
    return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "SearchCriteriaProperties"]


def _overrides():
    """UBW_CRITERIA="col=value; col2=from..to; col3=" -> {col: (from, to)}. Empty = no restriction."""
    out = {}
    for part in filter(None, (p.strip() for p in os.getenv("UBW_CRITERIA", "").split(";"))):
        col, _, val = part.partition("=")
        lo, _, hi = val.partition("..")
        out[col.strip().lower()] = (lo.strip(), hi.strip())
    return out


def _criteria_xml(tid):
    """Template's own criteria with UBW_CRITERIA applied, as SOAP XML ('' if no overrides)."""
    ov = _overrides()
    if not ov:
        return ""
    props, used = [], set()
    for p in criteria(tid):
        fields = [(c.tag.rsplit("}", 1)[-1], c.text) for c in p]
        col = (dict(fields).get("ColumnName") or "").lower()
        if col in ov:
            used.add(col)
            lo, hi = ov[col]
            if not lo and not hi:  # empty = remove the restriction entirely
                fields = [(k, None if k in ("FromValue", "ToValue", "RestrictionType") else v)
                          for k, v in fields]
            else:
                fields = [(k, lo if k == "FromValue" else hi if k == "ToValue" else v)
                          for k, v in fields]
        props.append("<q:SearchCriteriaProperties>" + "".join(
            f"<q:{k}>{escape(v)}</q:{k}>" for k, v in fields if v is not None
        ) + "</q:SearchCriteriaProperties>")
    missing = set(ov) - used
    if missing:
        raise ValueError(f"UBW_CRITERIA columns not in template {tid}: {sorted(missing)} "
                         f"(see: python -m connectors.ubw criteria)")
    return "<q:SearchCriteriaPropertiesList>" + "".join(props) + "</q:SearchCriteriaPropertiesList>"


def fetch_report():
    """Run UBW_TEMPLATE_ID and return all rows as a list of dicts (column -> text)."""
    tid = int(os.environ["UBW_TEMPLATE_ID"])
    # WCF expects members in alphabetical order: SearchCriteriaPropertiesList before TemplateId.
    root = _call("GetTemplateResultAsDataSet",
                 f"<q:input>{_criteria_xml(tid)}<q:TemplateId>{tid}</q:TemplateId></q:input>")
    code, err = _text(root, "ReturnCode"), _text(root, "Status")
    if code not in (None, "0"):
        raise RuntimeError(f"UBW template {tid}: ReturnCode {code}: {err}")
    global last_response
    last_response = ET.tostring(root, encoding="unicode")
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
    elif cmd == "criteria":
        for p in criteria(int(os.environ["UBW_TEMPLATE_ID"])):
            d = {c.tag.rsplit("}", 1)[-1]: c.text for c in p}
            print({k: d.get(k) for k in ("ColumnName", "Description", "RestrictionType",
                                        "FromValue", "ToValue", "DataType", "IsParameter")})
    elif cmd == "columns":
        rows = fetch_report()
        print(len(rows), "rows; columns:", list(rows[0]) if rows else [])
        for r in rows[:3]:
            print(r)
        if rows:
            st = {}
            for r in rows:
                st[r.get("status")] = st.get(r.get("status"), 0) + 1
            print("status counts:", st)
        else:
            print("UBW response (first 1500 chars):", last_response[:1500])
