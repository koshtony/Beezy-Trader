"""A dependency-free scheduler for running Beezy Trader unattended.

Schedule (Africa/Nairobi time):
  * Mon–Fri 15:30  daily refresh: prices → news → analysis (fundamentals if older than 7 days)
  * Mon–Fri 10:00, 13:00, 18:00  headline refresh + quick re-analysis
  * Saturday 08:00  fundamentals refresh + analysis, and backfill of any newly listed counters

Prefer cron in production (see README); this is handy on a laptop or a single small server.
"""

import datetime as dt
import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from market.services import ingest, pipeline

log = logging.getLogger("market.scheduler")

JOBS = [
    # (weekdays, hour, minute, name)
    ({0, 1, 2, 3, 4}, 15, 30, "daily"),
    ({0, 1, 2, 3, 4}, 10, 0, "news"),
    ({0, 1, 2, 3, 4}, 13, 0, "news"),
    ({0, 1, 2, 3, 4}, 18, 0, "news"),
    ({5}, 8, 0, "weekly"),
]


def run(name: str):
    if name == "daily":
        return pipeline.daily_refresh()
    if name == "news":
        ingest.update_news()
        pipeline.run_analysis(with_backtest=False)
        return "news refreshed"
    if name == "weekly":
        ingest.update_fundamentals()
        ingest.backfill_history(only_missing=True)
        pipeline.run_analysis()
        return "fundamentals refreshed"
    raise ValueError(name)


class Command(BaseCommand):
    help = "Run the built-in scheduler loop (Ctrl+C to stop)."

    def add_arguments(self, parser):
        parser.add_argument("--now", choices=["daily", "news", "weekly"], help="Run one job immediately and exit")

    def handle(self, *args, **opts):
        if opts["now"]:
            self.stdout.write(str(run(opts["now"])))
            return
        self.stdout.write("Beezy scheduler running (Africa/Nairobi). Ctrl+C to stop.")
        done: set[tuple[dt.date, str, int, int]] = set()
        while True:
            now = timezone.localtime()
            for days, hh, mm, name in JOBS:
                key = (now.date(), name, hh, mm)
                due = now.weekday() in days and (now.hour, now.minute) >= (hh, mm) and (now.hour - hh) * 60 + now.minute - mm < 30
                if due and key not in done:
                    done.add(key)
                    try:
                        log.info("scheduler: %s → %s", name, run(name))
                    except Exception:
                        log.exception("scheduler job %s failed", name)
            done = {k for k in done if k[0] >= now.date() - dt.timedelta(days=1)}
            time.sleep(30)
