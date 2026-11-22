from django.core.management.base import BaseCommand

from market.services import pipeline

from ._base import job_lock


class Command(BaseCommand):
    help = (
        "Scheduled job (cron): scrape prices, refresh news (and fundamentals if stale), then re-run the "
        "analysis if anything changed. Waits for a refresh already running in the web server."
    )

    def add_arguments(self, parser):
        parser.add_argument("--skip-news", action="store_true")
        parser.add_argument("--full-news", action="store_true", help="Query every stock over 60 days")
        parser.add_argument("--fundamentals", action="store_true", help="Force a fundamentals refresh")
        parser.add_argument("--wait", type=int, default=900, help="Seconds to wait for a running job (default 900)")

    def handle(self, *args, **opts):
        with job_lock(self, opts["wait"]):
            summary = pipeline.daily_refresh(
                news=not opts["skip_news"],
                force_fundamentals=opts["fundamentals"],
                full_news=True if opts["full_news"] else None,
            )
        self.stdout.write(self.style.SUCCESS(summary))
