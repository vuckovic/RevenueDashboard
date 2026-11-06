"""FX rates to EUR, stored in Postgres table fx_rates (one row per currency).

Refreshed at the start of every sync from the ECB daily reference rates
(https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml, no API key).
FX_TO_EUR in .env ("USD=0.92,SEK=0.087") is only a fallback for currencies the
ECB doesn't publish. If the ECB can't be reached, the last stored rates are kept.

rate_to_eur = how many EUR one unit of the currency is worth (EUR = 1).
"""
import os
import xml.etree.ElementTree as ET

import requests

import db

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def fetch_ecb():
    """-> (rate_date, {currency: rate_to_eur}). ECB publishes 1 EUR = x currency."""
    resp = requests.get(ECB_URL, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    day = next(e for e in root.iter() if e.tag.endswith("Cube") and e.get("time"))
    rates = {e.get("currency"): 1 / float(e.get("rate")) for e in day if e.get("currency")}
    return day.get("time"), rates


def env_fallback():
    out = {}
    for part in os.getenv("FX_TO_EUR", "").split(","):
        k, _, v = part.partition("=")
        try:
            out[k.strip().upper()] = float(v)
        except ValueError:
            pass
    return out


def refresh():
    """Update fx_rates; never raises (a failed FX refresh must not stop the data sync)."""
    rows = [("EUR", 1.0, None, "EUR")]
    try:
        day, ecb = fetch_ecb()
        rows += [(cur, rate, day, "ECB") for cur, rate in ecb.items()]
        msg = f"fx: {len(ecb)} ECB rates of {day}"
    except Exception as e:  # keep the last stored rates
        ecb, msg = {}, f"fx: ECB not reachable ({e}) - keeping stored rates"
    fallback = {k: v for k, v in env_fallback().items() if k not in ecb and k != "EUR"}
    rows += [(cur, rate, None, "FX_TO_EUR") for cur, rate in fallback.items()]
    if fallback:
        msg += (f", .env fallback for: {', '.join(sorted(fallback))}"
                + ("" if ecb else " (only where no ECB rate is stored)"))
    try:
        with db.connect() as c, c.transaction(), c.cursor() as cur:
            cur.execute(db.SCHEMA)
            cur.executemany(
                """INSERT INTO fx_rates (currency, rate_to_eur, rate_date, source, updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (currency) DO UPDATE SET rate_to_eur = EXCLUDED.rate_to_eur,
                       rate_date = EXCLUDED.rate_date, source = EXCLUDED.source, updated_at = now()
                   -- an .env fallback never overwrites a stored ECB rate (e.g. while ECB is down)
                   WHERE EXCLUDED.source <> 'FX_TO_EUR' OR fx_rates.source <> 'ECB'""",
                rows)
    except Exception as e:
        msg += f" | not saved: {e}"
    print(msg, flush=True)
    return msg


def rates():
    """{currency: rate_to_eur} as stored."""
    with db.connect() as c:
        return {cur: float(r) for cur, r in c.execute("SELECT currency, rate_to_eur FROM fx_rates")}
