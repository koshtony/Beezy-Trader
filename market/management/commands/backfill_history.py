from market.services import ingest

from ._base import TickerCommand, job_lock


class Command(TickerCommand):
    help = (
        "Download ~10 years of daily closes per stock from afx.kwayisi.org. "
        "Honours the site's 60s crawl delay, so a full run takes about an hour."
    )

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--missing", action="store_true", help="Only stocks never backfilled")

    def handle(self, *args, **opts):
        with job_lock(self):
            n = ingest.backfill_history(opts["tickers"] or None, only_missing=opts["missing"])
        self.stdout.write(self.style.SUCCESS(f"Stored {n} historical closes"))
