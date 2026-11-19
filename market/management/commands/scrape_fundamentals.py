from market.services import ingest

from ._base import TickerCommand


class Command(TickerCommand):
    help = "Scrape valuation, profitability, cash-flow, debt and dividend inputs (stockanalysis.com)."

    def handle(self, *args, **opts):
        n = ingest.update_fundamentals(opts["tickers"] or None)
        self.stdout.write(self.style.SUCCESS(f"Updated fundamentals for {n} stocks"))
