from django.core.management.base import BaseCommand

from market.services import pipeline


class Command(BaseCommand):
    help = "Recompute metrics, projections, scores and the top-5 recommendations."

    def handle(self, *args, **opts):
        run = pipeline.run_analysis()
        picks = ", ".join(p["ticker"] for p in run.payload.get("top5", []))
        self.stdout.write(self.style.SUCCESS(f"Analysis complete. Top 5: {picks or '—'}"))
