"""Run the refresh schedule in the foreground (08:30, 12:30, 17:30 Africa/Nairobi, every day).

In production the same scheduler runs inside the web server (BEEZY_SCHEDULER=1, see gunicorn.conf.py);
this command is for laptops or a separate worker. Fundamentals refresh when older than 7 days, and a
full 60-day news sweep runs weekly; other news runs only fetch the days since the last run.
"""

from django.core.management.base import BaseCommand

from market.services import ingest, pipeline, scheduler

from ._base import job_lock


def run(cmd: BaseCommand, name: str) -> str:
    with job_lock(cmd):
        if name == "daily":
            return pipeline.daily_refresh()
        if name == "news":
            ingest.update_news()
            pipeline.run_analysis(with_backtest=False)
            return "news refreshed"
        if name == "weekly":
            ingest.update_fundamentals()
            ingest.backfill_history(only_missing=True)
            ingest.update_news(full=True)
            pipeline.run_analysis()
            return "fundamentals, history and full news refreshed"
    raise ValueError(name)


class Command(BaseCommand):
    help = "Run the built-in refresh scheduler (Ctrl+C to stop)."

    def add_arguments(self, parser):
        parser.add_argument("--now", choices=["daily", "news", "weekly"], help="Run one job immediately and exit")

    def handle(self, *args, **opts):
        if opts["now"]:
            self.stdout.write(str(run(self, opts["now"])))
            return
        self.stdout.write("Beezy scheduler running (Africa/Nairobi). Ctrl+C to stop.")
        try:
            scheduler.loop()
        except KeyboardInterrupt:
            pass
