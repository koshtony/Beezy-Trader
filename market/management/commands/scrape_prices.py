from django.core.management.base import BaseCommand

from market.services import ingest


class Command(BaseCommand):
    help = "Scrape today's NSE closing prices (one request to afx.kwayisi.org) and store them."

    def handle(self, *args, **opts):
        n = ingest.update_daily_prices()
        self.stdout.write(self.style.SUCCESS(f"Stored {n} daily closes"))
