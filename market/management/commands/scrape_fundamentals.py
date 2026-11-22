from market.services import ingest

from ._base import TickerCommand, job_lock


class Command(TickerCommand):
    help = "Scrape valuation, profitability, cash-flow, debt and dividend inputs (stockanalysis.com)."

    def handle(self, *args, **opts):
        with job_lock(self):
            n = ingest.update_fundamentals(opts["tickers"] or None)
        self.stdout.write(self.style.SUCCESS(f"Updated fundamentals for {n} stocks"))
