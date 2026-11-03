"""Scheduler: runs a full sync every SYNC_INTERVAL_MINUTES."""
import os
import time

import db
import sync

if __name__ == "__main__":
    for _ in range(30):
        try:
            db.init()
            break
        except Exception:
            time.sleep(2)
    interval = int(os.getenv("SYNC_INTERVAL_MINUTES", "60")) * 60
    while True:
        print("sync start", flush=True)
        sync.run_all()
        print("sync done", flush=True)
        time.sleep(interval)
