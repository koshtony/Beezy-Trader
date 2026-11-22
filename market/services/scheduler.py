"""Refresh schedule: 08:30, 12:30 and 17:30 Africa/Nairobi, every day.

State lives in the database (JobLog), not memory, so restarts neither repeat a slot nor skip one:
a slot counts as done once a refresh has started after it, and a slot missed while the server was
down runs as soon as it is back. Used by the in-process scheduler thread (gunicorn, see
gunicorn.conf.py) and by `manage.py run_scheduler`; cron users simply call `manage.py refresh`.
"""

import datetime as dt
import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import JobLog
from . import locks, pipeline

log = logging.getLogger("market.scheduler")

SLOTS = [dt.time(8, 30), dt.time(12, 30), dt.time(17, 30)]
TICK_SECONDS = 60
FIRST_TICK_DELAY = 20  # let the web server finish booting first


def _at(day: dt.date, t: dt.time) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(day, t))


def last_slot(now: dt.datetime | None = None) -> dt.datetime:
    local = timezone.localtime(now or timezone.now())
    for back in (0, 1):
        day = local.date() - dt.timedelta(days=back)
        for t in reversed(SLOTS):
            when = _at(day, t)
            if when <= local:
                return when
    raise AssertionError("unreachable: yesterday's last slot is always in the past")


def next_slot(now: dt.datetime | None = None) -> dt.datetime:
    local = timezone.localtime(now or timezone.now())
    for ahead in (0, 1):
        day = local.date() + dt.timedelta(days=ahead)
        for t in SLOTS:
            when = _at(day, t)
            if when > local:
                return when
    raise AssertionError("unreachable")


def due(now: dt.datetime | None = None) -> dt.datetime | None:
    """The slot that still needs a refresh, if any."""
    slot = last_slot(now)
    return None if JobLog.objects.filter(job="refresh", started_at__gte=slot).exists() else slot


def tick(now: dt.datetime | None = None) -> str | None:
    """Run whatever is due. Returns a summary, or None when idle or another job holds the lock."""
    if settings.BEEZY["AUTO_BOOTSTRAP"] and pipeline.needs_bootstrap():
        if locks.acquire_jobs(0):
            try:
                log.info("empty database: starting bootstrap (about 75 minutes)")
                return pipeline.bootstrap()
            finally:
                locks.release_jobs()
        return None
    slot = due(now)
    if slot is None:
        return None
    if not locks.acquire_jobs(0):  # e.g. someone pressed "Update prices"; try again next tick
        return None
    try:
        log.info("refresh for the %s slot", timezone.localtime(slot).strftime("%a %H:%M"))
        return pipeline.daily_refresh()
    finally:
        locks.release_jobs()


def loop(stop: threading.Event | None = None) -> None:
    """Run forever (or until `stop` is set). Only one scheduler per database: extra web workers wait."""
    stop = stop or threading.Event()
    while not locks.SCHEDULER.acquire(0):
        if stop.wait(300):
            return
    try:
        log.info("scheduler on: refreshes at %s (Africa/Nairobi)", ", ".join(t.strftime("%H:%M") for t in SLOTS))
        if stop.wait(FIRST_TICK_DELAY):
            return
        while not stop.is_set():
            try:
                close_old_connections()
                result = tick()
                if result:
                    log.info("scheduler: %s · next at %s", result, timezone.localtime(next_slot()).strftime("%a %H:%M"))
            except Exception:
                log.exception("scheduled refresh failed")
            finally:
                close_old_connections()
            stop.wait(TICK_SECONDS)
    finally:
        locks.SCHEDULER.release()


_thread: threading.Thread | None = None


def start_thread() -> threading.Thread:
    """Start the scheduler in a daemon thread of this process (idempotent)."""
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=loop, daemon=True, name="beezy-scheduler")
        _thread.start()
    return _thread
