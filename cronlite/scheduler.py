"""
cronlite.scheduler
------------------
The main Scheduler class that manages jobs and runs them in a background thread.
"""

import time
import logging
import threading
from datetime import datetime
from typing import Callable, List, Optional

from .core import Job, JobStore, parse_schedule, next_run_time

logger = logging.getLogger("cronlite")


class Scheduler:
    """
    Embedded cron-like task scheduler.

    Runs all registered jobs in a single background daemon thread.
    Persists job state and run history to SQLite.

    Example
    -------
    >>> scheduler = Scheduler(db_path="jobs.db")
    >>>
    >>> @scheduler.schedule("every 5 minutes")
    ... def cleanup():
    ...     purge_old_files()
    >>>
    >>> @scheduler.schedule("daily at 09:00")
    ... def morning_report():
    ...     send_email_report()
    >>>
    >>> scheduler.start()
    >>> # ... your app runs ...
    >>> scheduler.stop()
    """

    def __init__(
        self,
        db_path: str = "cronlite.db",
        tick_interval: float = 1.0,
        catch_up_missed: bool = True,
    ):
        """
        Parameters
        ----------
        db_path : str
            Path to the SQLite database file. Default: "cronlite.db".
        tick_interval : float
            How often (seconds) the scheduler checks for due jobs. Default: 1.0.
        catch_up_missed : bool
            If True, a job whose last_run + interval is in the past when the
            scheduler starts will be run immediately (missed-run detection).
            Default: True.
        """
        self._store = JobStore(db_path)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._tick = tick_interval
        self._catch_up = catch_up_missed
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ── Job Registration ──────────────────────────────────────────────────────

    def schedule(
        self,
        expr: str,
        *,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        max_retries: int = 0,
    ) -> Callable:
        """
        Decorator that registers a function as a scheduled job.

        Parameters
        ----------
        expr : str
            Schedule expression. Examples:
              "every 5 minutes", "every 30 seconds", "daily at 09:00",
              "hourly", "every day", "0 9 * * 1-5"
        name : str, optional
            Job name. Defaults to the function name.
        tags : list[str], optional
            Tags for grouping/filtering jobs.
        max_retries : int
            Retry the job up to this many times on failure. Default: 0.

        Example
        -------
        >>> @scheduler.schedule("every 10 minutes", tags=["cleanup"])
        ... def purge_cache():
        ...     cache.clear()
        """
        def decorator(func: Callable) -> Callable:
            job_name = name or func.__name__
            job = Job(job_name, func, expr, tags=tags, max_retries=max_retries)

            # Restore state from DB if this job has run before
            saved = self._store.load_job_state(job_name)
            if saved:
                job.run_count = saved["run_count"]
                job.error_count = saved["error_count"]
                job.enabled = bool(saved["enabled"])
                if saved["last_run"]:
                    job.last_run = datetime.fromisoformat(saved["last_run"])
                if saved["next_run"]:
                    job.next_run = datetime.fromisoformat(saved["next_run"])

                # Missed-run detection
                if self._catch_up and datetime.now() > job.next_run:
                    logger.info(
                        "[cronlite] Job '%s' missed its scheduled run at %s — "
                        "running immediately on start.", job_name, job.next_run
                    )
                    job.next_run = datetime.now()

            with self._lock:
                self._jobs[job_name] = job

            self._store.upsert_job(job)
            logger.info("[cronlite] Registered job '%s' | %s | next: %s",
                        job_name, expr, job.next_run)
            return func

        return decorator

    def add(
        self,
        func: Callable,
        expr: str,
        name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        max_retries: int = 0,
    ) -> Job:
        """
        Register a job without using the decorator syntax.

        Returns the Job object.
        """
        decorated = self.schedule(
            expr, name=name, tags=tags, max_retries=max_retries
        )(func)
        job_name = name or func.__name__
        return self._jobs[job_name]

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self, block: bool = False):
        """
        Start the scheduler in a background thread.

        Parameters
        ----------
        block : bool
            If True, block the calling thread (useful for standalone scripts).
            If False (default), returns immediately and runs in background.
        """
        if self._running:
            logger.warning("[cronlite] Scheduler is already running.")
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="cronlite-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("[cronlite] Scheduler started (%d jobs registered).", len(self._jobs))

        if block:
            try:
                while self._running:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        """Gracefully stop the scheduler."""
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._store.close()
        logger.info("[cronlite] Scheduler stopped.")

    def pause(self, name: str):
        """Temporarily disable a job by name."""
        with self._lock:
            job = self._jobs.get(name)
        if not job:
            raise KeyError(f"No job named {name!r}")
        job.enabled = False
        self._store.upsert_job(job)
        logger.info("[cronlite] Job '%s' paused.", name)

    def resume(self, name: str):
        """Re-enable a previously paused job."""
        with self._lock:
            job = self._jobs.get(name)
        if not job:
            raise KeyError(f"No job named {name!r}")
        job.enabled = True
        job.next_run = next_run_time(job.schedule)
        self._store.upsert_job(job)
        logger.info("[cronlite] Job '%s' resumed. Next run: %s", name, job.next_run)

    def cancel(self, name: str):
        """Remove a job entirely from the scheduler."""
        with self._lock:
            removed = self._jobs.pop(name, None)
        if not removed:
            raise KeyError(f"No job named {name!r}")
        logger.info("[cronlite] Job '%s' cancelled.", name)

    def run_now(self, name: str):
        """Trigger a job immediately, outside of its schedule."""
        with self._lock:
            job = self._jobs.get(name)
        if not job:
            raise KeyError(f"No job named {name!r}")
        self._execute(job)

    # ── Introspection ─────────────────────────────────────────────────────────

    def jobs(self) -> List[Job]:
        """Return all registered Job objects."""
        with self._lock:
            return list(self._jobs.values())

    def get_job(self, name: str) -> Job:
        """Return a specific Job by name."""
        with self._lock:
            job = self._jobs.get(name)
        if not job:
            raise KeyError(f"No job named {name!r}")
        return job

    def status(self) -> List[dict]:
        """Return status dicts for all jobs."""
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.status() for j in jobs]

    def history(self, name: str, limit: int = 20) -> List[dict]:
        """Return the run history for a job."""
        return self._store.get_history(name, limit=limit)

    def print_status(self):
        """Print a formatted status table to stdout."""
        rows = self.status()
        if not rows:
            print("No jobs registered.")
            return
        print(f"\n{'Job':<25} {'Schedule':<22} {'Enabled':<9} {'Runs':<7} {'Errors':<8} Next Run")
        print("-" * 95)
        for r in rows:
            print(
                f"{r['name']:<25} {r['schedule']:<22} {str(r['enabled']):<9} "
                f"{r['run_count']:<7} {r['error_count']:<8} {r['next_run']}"
            )
        print()

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                due_jobs = [j for j in self._jobs.values() if j.is_due()]

            for job in due_jobs:
                t = threading.Thread(
                    target=self._execute, args=(job,),
                    name=f"cronlite-{job.name}", daemon=True
                )
                t.start()

            self._stop_event.wait(self._tick)

    def _execute(self, job: Job):
        started = datetime.now()
        logger.info("[cronlite] Running job '%s'...", job.name)
        success = job.run()
        finished = datetime.now()

        self._store.upsert_job(job)
        self._store.record_run(
            job.name, started, finished, success, job.last_error
        )

        if success:
            duration = (finished - started).total_seconds()
            logger.info(
                "[cronlite] Job '%s' completed in %.3fs. Next: %s",
                job.name, duration, job.next_run
            )
        else:
            logger.error(
                "[cronlite] Job '%s' failed. Next: %s", job.name, job.next_run
            )
