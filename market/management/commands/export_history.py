from django.core.management.base import BaseCommand

from market.services import ingest


class Command(BaseCommand):
    help = (
        "Export every stock and daily close to market/data/ (stocks.csv, price_history.csv.gz). "
        "Commit them: a new server with an empty database imports them instead of downloading history."
    )

    def handle(self, *args, **opts):
        stocks, closes = ingest.export_history()
        size = (ingest.BUNDLE_DIR / "price_history.csv.gz").stat().st_size / 1e6
        self.stdout.write(self.style.SUCCESS(f"Exported {stocks} counters and {closes} closes ({size:.1f} MB) to {ingest.BUNDLE_DIR}"))
