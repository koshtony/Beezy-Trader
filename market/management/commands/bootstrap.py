import threading

from django.core.management.base import BaseCommand

from market.services import ingest, pipeline


class Command(BaseCommand):
    help = (
        "First-time setup: daily prices, fundamentals, news, ~10y history, then analysis. "
        "History respects AFX's 60s crawl delay (≈75 min); fundamentals/news run in parallel."
    )

    def add_arguments(self, parser):
        parser.add_argument("--no-history", action="store_true")

    def handle(self, *args, **opts):
        ingest.update_daily_prices()
        side = threading.Thread(target=self._side_jobs, daemon=True)
        side.start()
        if not opts["no_history"]:
            ingest.backfill_history(only_missing=True)
        side.join()
        run = pipeline.run_analysis()
        self.stdout.write(self.style.SUCCESS(f"Bootstrap done. Top 5: {[p['ticker'] for p in run.payload.get('top5', [])]}"))

    def _side_jobs(self):
        from django.db import connection

        try:
            ingest.update_fundamentals()
            ingest.update_news()
        finally:
            connection.close()
