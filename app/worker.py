"""Scheduler: runs a sync every hour on the hour within SYNC_HOURS (local time SYNC_TZ).

Defaults: 08:00, 09:00 ... 17:00 Europe/Belgrade, every day
(set SYNC_WEEKDAYS_ONLY=true for Mon-Fri only).
"""
import datetime as dt
import os
import time
from zoneinfo import ZoneInfo

import db
import sync

TZ = ZoneInfo(os.getenv("SYNC_TZ", "Europe/Belgrade"))
FIRST, LAST = (int(h) for h in os.getenv("SYNC_HOURS", "8-17").split("-"))
WEEKDAYS_ONLY = os.getenv("SYNC_WEEKDAYS_ONLY", "false").lower() == "true"


def next_run(now):
    """Next full hour within the window (strictly after now)."""
    t = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    while not (FIRST <= t.hour <= LAST and (not WEEKDAYS_ONLY or t.weekday() < 5)):
        t += dt.timedelta(hours=1)
    return t


if __name__ == "__main__":
    for _ in range(30):
        try:
            db.init()
            break
        except Exception:
            time.sleep(2)
    while True:
        now = dt.datetime.now(TZ)
        nxt = next_run(now)
        print(f"next sync at {nxt:%Y-%m-%d %H:%M %Z}", flush=True)
        time.sleep(max(0, (nxt.astimezone(dt.timezone.utc) - now.astimezone(dt.timezone.utc)).total_seconds()))
        print(f"sync start {dt.datetime.now(TZ):%H:%M}", flush=True)
        sync.run_all()
        print("sync done", flush=True)
