"""Build an .xlsx of invoices for download from the web interface."""
import io

from openpyxl import Workbook
from openpyxl.worksheet.table import Table, TableStyleInfo


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
