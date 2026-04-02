"""
cronlite.core
-------------
Embedded Python task scheduler with human-readable schedule strings,
cron expression support, background thread execution, and SQLite persistence.
"""

import re
import time
import sqlite3
import logging
import threading
import traceback
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("cronlite")


# ── Schedule Parsing ──────────────────────────────────────────────────────────

_UNITS: Dict[str, int] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_EVERY_RE = re.compile(r"^every\s+(\d+)\s+(\w+)$", re.IGNORECASE)
_EVERY_UNIT_RE = re.compile(r"^every\s+(\w+)$", re.IGNORECASE)


def parse_schedule(expr: str) -> dict:
    """
    Parse a human-readable schedule string or cron expression.

    Supported formats:
      "every 5 minutes"
      "every 30 seconds"
      "every 2 hours"
      "every day"
      "every week"
      "daily at 09:00"
      "daily at 14:30:00"
      "hourly"
      "minutely"
      "0 9 * * *"   (standard cron)

    Returns a dict with keys:
      type: "interval" | "cron" | "daily"
      interval_seconds: int   (for interval type)
      cron_parts: list        (for cron type)
      time_of_day: (h, m, s)  (for daily type)
    """
    expr = expr.strip()

    # Cron expression (5 fields)
    parts = expr.split()
    if len(parts) == 5 and all(
        re.match(r'^[\d\*,\-/]+$', p) for p in parts
    ):
        return {"type": "cron", "cron_parts": parts, "original": expr}

    lower = expr.lower()

    # "hourly"
    if lower == "hourly":
        return {"type": "interval", "interval_seconds": 3600, "original": expr}

    # "minutely"
    if lower == "minutely":
        return {"type": "interval", "interval_seconds": 60, "original": expr}

    # "every 5 minutes" / "every 2 hours" etc.
    m = _EVERY_RE.match(lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit not in _UNITS:
            raise ValueError(f"Unknown time unit: {unit!r}")
        return {"type": "interval", "interval_seconds": n * _UNITS[unit], "original": expr}

    # "every day" / "every week" / "every minute" / "every hour"
    m = _EVERY_UNIT_RE.match(lower)
    if m:
        unit = m.group(1)
        if unit not in _UNITS:
            raise ValueError(f"Unknown time unit: {unit!r}")
        return {"type": "interval", "interval_seconds": _UNITS[unit], "original": expr}

    # "daily at HH:MM" / "daily at HH:MM:SS"
    daily_match = re.match(r"^daily\s+at\s+(.+)$", lower)
    if daily_match:
        t = daily_match.group(1).strip()
        tm = _TIME_RE.match(t)
        if not tm:
            raise ValueError(f"Cannot parse time: {t!r}. Use HH:MM or HH:MM:SS")
        h, m_, s = int(tm.group(1)), int(tm.group(2)), int(tm.group(3) or 0)
        if not (0 <= h < 24 and 0 <= m_ < 60 and 0 <= s < 60):
            raise ValueError(f"Invalid time: {h:02d}:{m_:02d}:{s:02d}")
        return {"type": "daily", "time_of_day": (h, m_, s), "original": expr}

    raise ValueError(
        f"Cannot parse schedule: {expr!r}\n"
        "Valid formats: 'every N minutes/hours/days', 'daily at HH:MM', "
        "'hourly', 'minutely', or a 5-field cron string."
    )


def next_run_time(schedule: dict, after: Optional[datetime] = None) -> datetime:
    """Compute the next datetime this schedule should fire."""
    now = after or datetime.now()

    if schedule["type"] == "interval":
        return now + timedelta(seconds=schedule["interval_seconds"])

    if schedule["type"] == "daily":
        h, m, s = schedule["time_of_day"]
        candidate = now.replace(hour=h, minute=m, second=s, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule["type"] == "cron":
        return _next_cron(schedule["cron_parts"], now)

    raise ValueError(f"Unknown schedule type: {schedule['type']}")


def _next_cron(parts: List[str], after: datetime) -> datetime:
    """
    Minimal cron next-time calculator.
    Supports: * N N,M N-M */N
    Fields: minute hour day_of_month month day_of_week
    """
    def matches(field: str, value: int, min_v: int, max_v: int) -> bool:
        if field == "*":
            return True
        for segment in field.split(","):
            if "/" in segment:
                base, step = segment.split("/", 1)
                step = int(step)
                start = min_v if base == "*" else int(base)
                if value >= start and (value - start) % step == 0:
                    return True
            elif "-" in segment:
                a, b = segment.split("-", 1)
                if int(a) <= value <= int(b):
                    return True
            elif int(segment) == value:
                return True
        return False

    min_f, hr_f, dom_f, mon_f, dow_f = parts
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(366 * 24 * 60):  # max search: 1 year
        if (matches(mon_f, t.month, 1, 12) and
                matches(dom_f, t.day, 1, 31) and
                matches(dow_f, t.weekday(), 0, 6) and
                matches(hr_f, t.hour, 0, 23) and
                matches(min_f, t.minute, 0, 59)):
            return t
        t += timedelta(minutes=1)

    raise RuntimeError("Could not find next cron time within 1 year")


# ── Job ───────────────────────────────────────────────────────────────────────

class Job:
    """Represents a single scheduled task."""

    def __init__(
        self,
        name: str,
        func: Callable,
        schedule_expr: str,
        tags: Optional[List[str]] = None,
        max_retries: int = 0,
    ):
        self.name = name
        self.func = func
        self.schedule_expr = schedule_expr
        self.schedule = parse_schedule(schedule_expr)
        self.tags = tags or []
        self.max_retries = max_retries

        self.enabled: bool = True
        self.last_run: Optional[datetime] = None
        self.next_run: datetime = next_run_time(self.schedule)
        self.run_count: int = 0
        self.error_count: int = 0
        self.last_error: Optional[str] = None

    def is_due(self) -> bool:
        return self.enabled and datetime.now() >= self.next_run

    def run(self) -> bool:
        """Execute the job. Returns True on success."""
        self.last_run = datetime.now()
        self.run_count += 1
        success = False

        for attempt in range(self.max_retries + 1):
            try:
                self.func()
                success = True
                self.last_error = None
                break
            except Exception as e:
                self.error_count += 1
                self.last_error = traceback.format_exc()
                logger.error(
                    "[cronlite] Job '%s' failed (attempt %d/%d): %s",
                    self.name, attempt + 1, self.max_retries + 1, e
                )
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)

        self.next_run = next_run_time(self.schedule)
        return success

    def status(self) -> dict:
        return {
            "name": self.name,
            "schedule": self.schedule_expr,
            "enabled": self.enabled,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat(),
            "last_error": self.last_error,
            "tags": self.tags,
        }

    def __repr__(self):
        return (
            f"<Job name={self.name!r} schedule={self.schedule_expr!r} "
            f"enabled={self.enabled} next_run={self.next_run}>"
        )


# ── Persistence ───────────────────────────────────────────────────────────────

class JobStore:
    """SQLite-backed store for job run history and state."""

    def __init__(self, db_path: str = "cronlite.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    name        TEXT PRIMARY KEY,
                    schedule    TEXT NOT NULL,
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    run_count   INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    last_run    TEXT,
                    next_run    TEXT,
                    last_error  TEXT,
                    tags        TEXT,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS job_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name    TEXT NOT NULL,
                    started_at  TEXT NOT NULL,
                    finished_at TEXT,
                    success     INTEGER,
                    error       TEXT,
                    FOREIGN KEY(job_name) REFERENCES jobs(name)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_job ON job_runs(job_name);
                CREATE INDEX IF NOT EXISTS idx_runs_started ON job_runs(started_at);
            """)
            self._conn.commit()

    def upsert_job(self, job: Job):
        with self._lock:
            self._conn.execute("""
                INSERT INTO jobs (name, schedule, enabled, run_count, error_count,
                                  last_run, next_run, last_error, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    schedule    = excluded.schedule,
                    enabled     = excluded.enabled,
                    run_count   = excluded.run_count,
                    error_count = excluded.error_count,
                    last_run    = excluded.last_run,
                    next_run    = excluded.next_run,
                    last_error  = excluded.last_error,
                    tags        = excluded.tags
            """, (
                job.name, job.schedule_expr, int(job.enabled),
                job.run_count, job.error_count,
                job.last_run.isoformat() if job.last_run else None,
                job.next_run.isoformat(),
                job.last_error,
                ",".join(job.tags),
            ))
            self._conn.commit()

    def record_run(self, job_name: str, started_at: datetime,
                   finished_at: datetime, success: bool, error: Optional[str]):
        with self._lock:
            self._conn.execute("""
                INSERT INTO job_runs (job_name, started_at, finished_at, success, error)
                VALUES (?, ?, ?, ?, ?)
            """, (job_name, started_at.isoformat(), finished_at.isoformat(),
                  int(success), error))
            self._conn.commit()

    def load_job_state(self, job_name: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE name = ?", (job_name,)
            ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM jobs LIMIT 0").description]
        return dict(zip(cols, row))

    def get_history(self, job_name: str, limit: int = 20) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT job_name, started_at, finished_at, success, error
                FROM job_runs WHERE job_name = ?
                ORDER BY started_at DESC LIMIT ?
            """, (job_name, limit)).fetchall()
        return [
            {"job": r[0], "started_at": r[1], "finished_at": r[2],
             "success": bool(r[3]), "error": r[4]}
            for r in rows
        ]

    def all_jobs(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM jobs LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        self._conn.close()
