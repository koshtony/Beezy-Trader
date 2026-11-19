from django.core.management.base import BaseCommand

from market.services import pipeline


class Command(BaseCommand):
    help = (
        "Daily job: scrape prices, refresh news (and fundamentals if stale), then re-run analysis. "
        "Schedule after the NSE close (15:00 EAT), e.g. 15:30 Mon–Fri."
    )

    def add_arguments(self, parser):
        parser.add_argument("--skip-news", action="store_true")
        parser.add_argument("--fundamentals", action="store_true", help="Force a fundamentals refresh")

    def handle(self, *args, **opts):
        summary = pipeline.daily_refresh(
            news=not opts["skip_news"], force_fundamentals=opts["fundamentals"]
        )
        self.stdout.write(self.style.SUCCESS(summary))
