"""One data job at a time, across threads and processes (web workers, scheduler, cron, CLI).

A POSIX file lock next to the database does the cross-process part; the kernel drops it when a
process dies, so a crashed or redeployed job never leaves the lock stuck.
"""

import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.utils import timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: in-process locking only
    fcntl = None

log = logging.getLogger("market.locks")


class Busy(RuntimeError):
    """Another job holds the lock."""


def lock_path(name: str) -> Path:
    """Next to the database file (db.sqlite3-jobs.lock), so it is shared by everything using that DB."""
    db = str(settings.DATABASES["default"]["NAME"])
    if db == ":memory:" or db.startswith("file:"):  # test database: private to this test process
        return Path(tempfile.gettempdir()) / f"beezy-test-{os.getpid()}-{name}.lock"
    path = Path(db)
    return path.with_name(f"{path.name}-{name}.lock")


class FileLock:
    def __init__(self, name: str):
        self.name = name
        self._thread_lock = threading.Lock()
        self._fh = None

    def acquire(self, wait: float = 0.0) -> bool:
        deadline = time.monotonic() + max(wait, 0.0)
        got = self._thread_lock.acquire(timeout=wait) if wait > 0 else self._thread_lock.acquire(blocking=False)
        if not got:
            return False
        if fcntl is None:
            return True
        path = lock_path(self.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh = fh
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    fh.close()
                    self._thread_lock.release()
                    return False
                time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        self._thread_lock.release()

    def held(self) -> bool:
        return self._thread_lock.locked()


JOBS = FileLock("jobs")
SCHEDULER = FileLock("scheduler")


def acquire_jobs(wait: float = 0.0) -> bool:
    """Take the job lock; once held, any job still marked "running" was orphaned by a dead process."""
    if not JOBS.acquire(wait):
        return False
    try:
        _close_orphans()
    except Exception:  # never let bookkeeping block real work
        log.exception("could not tidy orphaned jobs")
    return True


def release_jobs() -> None:
    JOBS.release()


@contextmanager
def exclusive(wait: float = 0.0):
    if not acquire_jobs(wait):
        raise Busy("another refresh is running")
    try:
        yield
    finally:
        release_jobs()


def _close_orphans() -> None:
    from ..models import JobLog

    n = JobLog.objects.filter(status=JobLog.RUNNING).update(
        status=JobLog.FAILED, message="interrupted (server restarted)", finished_at=timezone.now()
    )
    if n:
        log.warning("marked %d interrupted job(s) as failed", n)
