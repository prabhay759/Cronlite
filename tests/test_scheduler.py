"""
Tests for cronlite.
Run with: pytest tests/ -v
"""

import os
import time
import tempfile
import threading
from datetime import datetime, timedelta

import pytest

from cronlite import Scheduler, parse_schedule, next_run_time
from cronlite.core import Job, JobStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def scheduler(db_path):
    s = Scheduler(db_path=db_path, tick_interval=0.05)
    yield s
    if s._running:
        s.stop()


@pytest.fixture
def store(db_path):
    s = JobStore(db_path)
    yield s
    s.close()


# ── parse_schedule ────────────────────────────────────────────────────────────

class TestParseSchedule:
    def test_every_n_minutes(self):
        r = parse_schedule("every 5 minutes")
        assert r["type"] == "interval"
        assert r["interval_seconds"] == 300

    def test_every_n_seconds(self):
        r = parse_schedule("every 30 seconds")
        assert r["type"] == "interval"
        assert r["interval_seconds"] == 30

    def test_every_n_hours(self):
        r = parse_schedule("every 2 hours")
        assert r["type"] == "interval"
        assert r["interval_seconds"] == 7200

    def test_every_day(self):
        r = parse_schedule("every day")
        assert r["type"] == "interval"
        assert r["interval_seconds"] == 86400

    def test_every_week(self):
        r = parse_schedule("every week")
        assert r["type"] == "interval"
        assert r["interval_seconds"] == 604800

    def test_hourly(self):
        r = parse_schedule("hourly")
        assert r["interval_seconds"] == 3600

    def test_minutely(self):
        r = parse_schedule("minutely")
        assert r["interval_seconds"] == 60

    def test_daily_at(self):
        r = parse_schedule("daily at 09:30")
        assert r["type"] == "daily"
        assert r["time_of_day"] == (9, 30, 0)

    def test_daily_at_with_seconds(self):
        r = parse_schedule("daily at 14:00:30")
        assert r["time_of_day"] == (14, 0, 30)

    def test_cron_expression(self):
        r = parse_schedule("0 9 * * 1-5")
        assert r["type"] == "cron"
        assert r["cron_parts"] == ["0", "9", "*", "*", "1-5"]

    def test_invalid_unit(self):
        with pytest.raises(ValueError, match="Unknown time unit"):
            parse_schedule("every 5 fortnights")

    def test_invalid_expression(self):
        with pytest.raises(ValueError):
            parse_schedule("run whenever you feel like it")

    def test_case_insensitive(self):
        r = parse_schedule("Every 10 Minutes")
        assert r["interval_seconds"] == 600


# ── next_run_time ─────────────────────────────────────────────────────────────

class TestNextRunTime:
    def test_interval(self):
        sched = parse_schedule("every 5 minutes")
        now = datetime(2025, 1, 1, 12, 0, 0)
        nxt = next_run_time(sched, after=now)
        assert nxt == datetime(2025, 1, 1, 12, 5, 0)

    def test_daily_future(self):
        sched = parse_schedule("daily at 18:00")
        now = datetime(2025, 6, 1, 10, 0, 0)
        nxt = next_run_time(sched, after=now)
        assert nxt == datetime(2025, 6, 1, 18, 0, 0)

    def test_daily_past_rolls_over(self):
        sched = parse_schedule("daily at 08:00")
        now = datetime(2025, 6, 1, 20, 0, 0)
        nxt = next_run_time(sched, after=now)
        assert nxt == datetime(2025, 6, 2, 8, 0, 0)

    def test_cron_weekday(self):
        sched = parse_schedule("0 9 * * *")
        now = datetime(2025, 6, 1, 8, 30, 0)
        nxt = next_run_time(sched, after=now)
        assert nxt.hour == 9 and nxt.minute == 0


# ── Job ───────────────────────────────────────────────────────────────────────

class TestJob:
    def test_run_success(self):
        results = []
        job = Job("test", lambda: results.append(1), "every 1 minute")
        success = job.run()
        assert success
        assert results == [1]
        assert job.run_count == 1

    def test_run_failure(self):
        def bad(): raise RuntimeError("oops")
        job = Job("bad", bad, "every 1 minute")
        success = job.run()
        assert not success
        assert job.error_count == 1
        assert "RuntimeError" in job.last_error

    def test_is_due(self):
        job = Job("due", lambda: None, "every 1 second")
        job.next_run = datetime.now() - timedelta(seconds=1)
        assert job.is_due()

    def test_not_due(self):
        job = Job("future", lambda: None, "every 1 minute")
        job.next_run = datetime.now() + timedelta(minutes=1)
        assert not job.is_due()

    def test_disabled_not_due(self):
        job = Job("disabled", lambda: None, "every 1 second")
        job.next_run = datetime.now() - timedelta(seconds=1)
        job.enabled = False
        assert not job.is_due()

    def test_next_run_updated_after_run(self):
        job = Job("next", lambda: None, "every 5 minutes")
        before = datetime.now()
        job.run()
        assert job.next_run > before

    def test_max_retries(self):
        attempts = {"n": 0}
        def flaky():
            attempts["n"] += 1
            raise RuntimeError("always fails")
        job = Job("retry", flaky, "every 1 minute", max_retries=2)
        job.run()
        assert attempts["n"] == 3  # 1 initial + 2 retries


# ── JobStore ──────────────────────────────────────────────────────────────────

class TestJobStore:
    def test_upsert_and_load(self, store):
        job = Job("stored", lambda: None, "every 5 minutes")
        store.upsert_job(job)
        data = store.load_job_state("stored")
        assert data is not None
        assert data["name"] == "stored"
        assert data["schedule"] == "every 5 minutes"

    def test_load_nonexistent(self, store):
        assert store.load_job_state("ghost") is None

    def test_record_and_get_history(self, store):
        job = Job("hist", lambda: None, "every 1 minute")
        store.upsert_job(job)
        now = datetime.now()
        store.record_run("hist", now, now, True, None)
        store.record_run("hist", now, now, False, "error!")
        history = store.get_history("hist")
        assert len(history) == 2
        assert history[0]["success"] is False  # most recent first
        assert history[1]["success"] is True

    def test_all_jobs(self, store):
        for i in range(3):
            job = Job(f"job{i}", lambda: None, "every 1 minute")
            store.upsert_job(job)
        all_jobs = store.all_jobs()
        assert len(all_jobs) == 3


# ── Scheduler ─────────────────────────────────────────────────────────────────

class TestScheduler:
    def test_register_via_decorator(self, scheduler):
        @scheduler.schedule("every 1 hour")
        def my_task():
            pass
        assert "my_task" in scheduler._jobs

    def test_register_via_add(self, scheduler):
        job = scheduler.add(lambda: None, "every 5 minutes", name="added_task")
        assert "added_task" in scheduler._jobs

    def test_job_runs_when_due(self, scheduler):
        results = []
        event = threading.Event()

        @scheduler.schedule("every 1 second")
        def quick_task():
            results.append(1)
            event.set()

        # Force it to run immediately
        scheduler._jobs["quick_task"].next_run = datetime.now()
        scheduler.start()
        event.wait(timeout=3)
        scheduler.stop()
        assert len(results) >= 1

    def test_pause_and_resume(self, scheduler):
        @scheduler.schedule("every 1 second")
        def pausable():
            pass

        scheduler.pause("pausable")
        assert not scheduler._jobs["pausable"].enabled

        scheduler.resume("pausable")
        assert scheduler._jobs["pausable"].enabled

    def test_cancel(self, scheduler):
        @scheduler.schedule("every 1 hour")
        def cancellable():
            pass

        scheduler.cancel("cancellable")
        assert "cancellable" not in scheduler._jobs

    def test_run_now(self, scheduler):
        results = []

        @scheduler.schedule("every 1 hour")
        def manual_task():
            results.append(1)

        scheduler.run_now("manual_task")
        assert results == [1]

    def test_status(self, scheduler):
        @scheduler.schedule("every 5 minutes")
        def status_task():
            pass

        statuses = scheduler.status()
        assert any(s["name"] == "status_task" for s in statuses)

    def test_history(self, scheduler):
        @scheduler.schedule("every 1 hour")
        def hist_task():
            pass

        scheduler.run_now("hist_task")
        hist = scheduler.history("hist_task")
        assert len(hist) == 1
        assert hist[0]["success"] is True

    def test_cancel_nonexistent_raises(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.cancel("nonexistent")

    def test_pause_nonexistent_raises(self, scheduler):
        with pytest.raises(KeyError):
            scheduler.pause("nonexistent")
