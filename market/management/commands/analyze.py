import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import Projection
from market.services import pipeline

from ._base import job_lock


class Command(BaseCommand):
    help = "Recompute metrics, projections, scores and the top-5 recommendations."

    def add_arguments(self, parser):
        parser.add_argument("--backtest", action="store_true", help="Recompute backtests even if today's exist")

    def handle(self, *args, **opts):
        with job_lock(self):
            if opts["backtest"]:  # forget reusable backtests so they are recomputed
                Projection.objects.filter(as_of__gte=timezone.localdate() - dt.timedelta(days=30)).update(backtest={})
            run = pipeline.run_analysis()
        picks = ", ".join(p["ticker"] for p in run.payload.get("top5", []))
        self.stdout.write(self.style.SUCCESS(f"Analysis complete. Top 5: {picks or '—'}"))
