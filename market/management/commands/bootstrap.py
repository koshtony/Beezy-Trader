from django.core.management.base import BaseCommand

from market.services import pipeline

from ._base import job_lock


class Command(BaseCommand):
    help = (
        "First-time setup: daily prices, fundamentals, news, ~10y history, then analysis. "
        "History respects AFX's 60s crawl delay (≈75 min, resumable); fundamentals/news run in parallel."
    )

    def add_arguments(self, parser):
        parser.add_argument("--no-history", action="store_true")

    def handle(self, *args, **opts):
        with job_lock(self):
            summary = pipeline.bootstrap(history=not opts["no_history"])
        self.stdout.write(self.style.SUCCESS(summary))
