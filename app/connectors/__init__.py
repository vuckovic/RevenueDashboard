"""Shared helpers for connectors."""
import hashlib
import json
import os
import re


def _norm(s):
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def find_field(rows, name):
    """Match a configured field name to the report's key, ignoring case/spaces/underscores."""
    if not name:
        return None
    keys = {k for r in rows for k in r}
    return next((k for k in keys if _norm(k) == _norm(name)), name)


def paid_rows(rows, prefix):
    """Filter by <PREFIX>_PAID_FIELD/_PAID_VALUE and key by <PREFIX>_ID_FIELD.
    Returns list of (key, row_dict)."""
    paid_field = find_field(rows, os.getenv(f"{prefix}_PAID_FIELD"))
    paid_value = os.getenv(f"{prefix}_PAID_VALUE", "Paid").strip().lower()
    if paid_field:
        rows = [r for r in rows if str(r.get(paid_field, "")).strip().lower() == paid_value]
    id_field = find_field(rows, os.getenv(f"{prefix}_ID_FIELD"))
    out = []
    for r in rows:
        key = str(r.get(id_field)) if id_field and r.get(id_field) else \
            hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()
        out.append((key, r))
    return out
