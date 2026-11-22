from contextlib import contextmanager

from django.core.management.base import BaseCommand, CommandError

from market.services import locks


@contextmanager
def job_lock(cmd: BaseCommand, wait: float = 1800):
    """Hold the job lock for a command, waiting (up to `wait` s) for a running refresh to finish."""
    if not locks.acquire_jobs(0):
        cmd.stdout.write("Another refresh is running; waiting for it to finish…")
        if not locks.acquire_jobs(wait):
            raise CommandError("Timed out waiting for the running job.")
    try:
        yield
    finally:
        locks.release_jobs()


class TickerCommand(BaseCommand):
    """Base for commands that accept an optional list of tickers."""

    def add_arguments(self, parser):
        parser.add_argument("tickers", nargs="*", help="Limit to these tickers (default: all)")
