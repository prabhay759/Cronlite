"""
cronlite — Examples
====================
Run this file to see cronlite in action:
    python examples/demo.py
"""

import logging
import time
import os
import tempfile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)

from cronlite import Scheduler

# Use a temp DB so the demo is self-contained
DB = os.path.join(tempfile.gettempdir(), "cronlite_demo.db")
scheduler = Scheduler(db_path=DB, tick_interval=0.5, catch_up_missed=True)

print("=" * 60)
print("  cronlite Demo")
print("=" * 60)

# ── Register jobs ─────────────────────────────────────────────────────────────

@scheduler.schedule("every 3 seconds", tags=["demo", "fast"])
def heartbeat():
    print("  💓 heartbeat — I run every 3 seconds")


@scheduler.schedule("every 7 seconds", tags=["demo"])
def cleanup():
    print("  🧹 cleanup — I run every 7 seconds")


@scheduler.schedule("every 10 seconds", max_retries=2, tags=["demo", "flaky"])
def flaky_job():
    import random
    if random.random() < 0.5:
        raise RuntimeError("Random failure! (will be retried)")
    print("  ✅ flaky_job — succeeded this time!")


# You can also add jobs without a decorator:
def report():
    print("  📊 report — added via scheduler.add()")

scheduler.add(report, "every 5 seconds", name="report")


# ── Show current status ───────────────────────────────────────────────────────
print("\nRegistered jobs:")
scheduler.print_status()


# ── Start the scheduler ───────────────────────────────────────────────────────
print("Starting scheduler for 20 seconds...\n")
scheduler.start()
time.sleep(5)


# ── Pause a job mid-run ───────────────────────────────────────────────────────
print("\n[Demo] Pausing 'cleanup' job...")
scheduler.pause("cleanup")
time.sleep(5)


# ── Resume it ─────────────────────────────────────────────────────────────────
print("\n[Demo] Resuming 'cleanup' job...")
scheduler.resume("cleanup")
time.sleep(5)


# ── Run a job manually ────────────────────────────────────────────────────────
print("\n[Demo] Triggering 'report' manually right now...")
scheduler.run_now("report")
time.sleep(5)


# ── Print final status ────────────────────────────────────────────────────────
print("\nFinal status:")
scheduler.print_status()


# ── Show run history ──────────────────────────────────────────────────────────
print("Run history for 'heartbeat' (last 5 runs):")
for run in scheduler.history("heartbeat", limit=5):
    status = "✅" if run["success"] else "❌"
    print(f"  {status} {run['started_at']}  →  {run['finished_at']}")


# ── Stop ──────────────────────────────────────────────────────────────────────
scheduler.stop()
print("\n✅ Demo complete!")
print("=" * 60)
