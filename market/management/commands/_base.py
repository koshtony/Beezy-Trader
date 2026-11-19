from django.core.management.base import BaseCommand


class TickerCommand(BaseCommand):
    """Base for commands that accept an optional list of tickers."""

    def add_arguments(self, parser):
        parser.add_argument("tickers", nargs="*", help="Limit to these tickers (default: all)")
