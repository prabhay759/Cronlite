# cronlite

> Embedded Python task scheduler — human-readable schedules, full cron support, SQLite persistence, missed-run detection, and zero dependencies.

[![PyPI version](https://img.shields.io/pypi/v/cronlite.svg)](https://pypi.org/project/cronlite/)
[![Python](https://img.shields.io/pypi/pyversions/cronlite.svg)](https://pypi.org/project/cronlite/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-passing-brightgreen.svg)]()

---

## Why cronlite?

Celery is overkill for most apps. System cron doesn't run inside your process. APScheduler is large and complex. `cronlite` is a small, self-contained scheduler you embed directly in your Python script or service:

- **Human-readable schedules** — `"every 5 minutes"`, `"daily at 09:00"`, `"hourly"` — no cron syntax required
- **Full cron expressions** for power users — `"0 9 * * 1-5"`
- **SQLite persistence** — job state and full run history survive restarts
- **Missed-run detection** — if your app was down when a job was due, it catches up on start
- **Pause, resume, cancel** individual jobs at runtime
- **Per-job retries** with exponential backoff
- **Background thread** — does not block your main app
- **Zero dependencies** — just the Python standard library

---

## Installation

```bash
pip install cronlite
```

No dependencies. Requires Python 3.8+.

---

## Quick Start

```python
from cronlite import Scheduler

scheduler = Scheduler(db_path="jobs.db")

@scheduler.schedule("every 5 minutes")
def cleanup_old_files():
    purge_temp_dir()

@scheduler.schedule("daily at 09:00")
def morning_report():
    send_email_report()

scheduler.start(block=True)  # Runs forever, Ctrl+C to stop
```

---

## Usage

### Scheduler Setup

```python
from cronlite import Scheduler

scheduler = Scheduler(
    db_path="my_jobs.db",       # SQLite file path (created automatically)
    tick_interval=1.0,          # How often to check for due jobs (seconds)
    catch_up_missed=True,       # Run missed jobs immediately on startup
)
```

### Schedule Expressions

| Expression | Meaning |
|---|---|
| `"every 30 seconds"` | Every 30 seconds |
| `"every 5 minutes"` | Every 5 minutes |
| `"every 2 hours"` | Every 2 hours |
| `"every day"` | Every 24 hours |
| `"every week"` | Every 7 days |
| `"hourly"` | Every hour |
| `"minutely"` | Every minute |
| `"daily at 09:00"` | Every day at 9:00 AM |
| `"daily at 14:30:00"` | Every day at 2:30:00 PM |
| `"0 9 * * 1-5"` | Weekdays at 9:00 AM (cron) |
| `"*/15 * * * *"` | Every 15 minutes (cron) |

### Decorator Syntax

```python
@scheduler.schedule("every 10 minutes", tags=["maintenance"], max_retries=2)
def vacuum_db():
    db.execute("VACUUM")
```

### Programmatic Registration

```python
def send_ping():
    requests.get("https://healthcheck.io/ping/xyz")

scheduler.add(send_ping, "every 5 minutes", name="healthcheck")
```

### Lifecycle Control

```python
scheduler.start()        # Start in background thread
scheduler.stop()         # Gracefully stop
scheduler.start(block=True)  # Block until Ctrl+C

scheduler.pause("my_job")   # Temporarily disable
scheduler.resume("my_job")  # Re-enable
scheduler.cancel("my_job")  # Remove entirely
scheduler.run_now("my_job") # Trigger immediately
```

### Introspection

```python
# Print a status table
scheduler.print_status()
# Job                      Schedule               Enabled   Runs    Errors   Next Run
# ─────────────────────────────────────────────────────────────────────────────────────
# cleanup_old_files        every 5 minutes        True      42      1        2025-06-01T09:05:00

# Get structured data
statuses = scheduler.status()   # List[dict]
all_jobs = scheduler.jobs()     # List[Job]
job      = scheduler.get_job("cleanup_old_files")

# Run history (from SQLite)
for run in scheduler.history("cleanup_old_files", limit=10):
    print(run["started_at"], run["success"], run["error"])
```

### Per-Job Retries

```python
@scheduler.schedule("every 1 hour", max_retries=3)
def flaky_api_call():
    # If this raises, cronlite retries up to 3 times
    # with exponential backoff (1s, 2s, 4s between retries)
    response = requests.post(...)
    response.raise_for_status()
```

### SQLite Persistence

All job state is stored in a local SQLite file. On restart:
- Job run counts and error counts are restored
- Missed runs are detected and executed immediately
- Full run history is preserved

```python
# The DB file contains two tables:
# - jobs: current state of each job
# - job_runs: full history of every execution
```

### Missed-Run Detection

```python
# If your app was down for 4 hours and a job runs every hour,
# cronlite will run it once immediately on startup (not 4 times).
scheduler = Scheduler(catch_up_missed=True)  # default
```

---

## API Reference

### `Scheduler`

| Method | Description |
|---|---|
| `__init__(db_path, tick_interval, catch_up_missed)` | Create scheduler |
| `schedule(expr, *, name, tags, max_retries)` | Decorator to register a job |
| `add(func, expr, name, tags, max_retries)` | Register without decorator |
| `start(block=False)` | Start background thread |
| `stop()` | Gracefully stop |
| `pause(name)` | Disable a job |
| `resume(name)` | Re-enable a job |
| `cancel(name)` | Remove a job |
| `run_now(name)` | Trigger immediately |
| `jobs()` | List all Job objects |
| `get_job(name)` | Get a specific Job |
| `status()` | List of status dicts |
| `history(name, limit)` | Run history from SQLite |
| `print_status()` | Pretty-print status table |

### `Job`

| Attribute | Type | Description |
|---|---|---|
| `name` | `str` | Job identifier |
| `schedule_expr` | `str` | Original schedule string |
| `enabled` | `bool` | Whether the job is active |
| `run_count` | `int` | Total successful runs |
| `error_count` | `int` | Total failed runs |
| `last_run` | `datetime \| None` | When it last ran |
| `next_run` | `datetime` | When it will run next |
| `last_error` | `str \| None` | Traceback from last failure |
| `tags` | `list[str]` | User-defined tags |

---

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

---

## License

MIT © prabhay759
