import unicodedata

from market.models import NewsArticle
from market.services import ingest
from market.services.sentiment import score_headline

from ._base import TickerCommand


class Command(TickerCommand):
    help = "Fetch recent headlines per stock (Google News RSS) and label them positive/negative."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--rescore", action="store_true", help="Only re-label stored headlines (after lexicon edits)"
        )

    def handle(self, *args, **opts):
        if opts["rescore"]:
            changed = 0
            for a in NewsArticle.objects.all():
                title = unicodedata.normalize("NFKC", a.title)
                score, label, terms = score_headline(title)
                if (title, score, label, terms) != (a.title, a.sentiment, a.label, a.matched_terms):
                    if title != a.title and NewsArticle.objects.filter(stock=a.stock, title=title).exists():
                        a.delete()  # normalised duplicate of an existing headline
                        continue
                    a.title, a.sentiment, a.label, a.matched_terms = title, score, label, terms
                    a.save(update_fields=["title", "sentiment", "label", "matched_terms"])
                    changed += 1
            self.stdout.write(self.style.SUCCESS(f"Re-scored {changed} headlines"))
            return
        n = ingest.update_news(opts["tickers"] or None)
        self.stdout.write(self.style.SUCCESS(f"Added {n} new headlines"))
