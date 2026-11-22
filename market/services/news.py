"""Company headlines from the Google News RSS feed (Kenya edition), scored for sentiment."""

import datetime as dt
import email.utils
import html
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import quote_plus

from django.conf import settings
from django.utils import timezone

from . import http
from ..security import safe_url
from .sentiment import score_headline

log = logging.getLogger("market.news")


@dataclass
class Headline:
    title: str
    url: str
    source: str
    published_at: dt.datetime
    score: float
    label: str
    terms: list[str]


FEED_CAP = 100  # Google News returns at most this many items per feed


def query(phrases: str | list[str], context: str = "", days: int | None = None) -> str:
    """`"A" OR "B" Kenya when:2d` — Google binds OR tighter than the implicit AND."""
    days = days or settings.BEEZY["NEWS_LOOKBACK_DAYS"]
    if isinstance(phrases, str):
        phrases = [phrases]
    names = " OR ".join(f'"{p}"' for p in phrases)
    return " ".join(x for x in (names, context, f"when:{days}d") if x)


def query_words(phrases: list[str], context: str = "") -> int:
    """Words a query uses (Google ignores anything past 32)."""
    return sum(len(p.split()) for p in phrases) + max(0, len(phrases) - 1) + len(context.split()) + 1


def feed_url(phrases: str | list[str], context: str = "", days: int | None = None) -> str:
    q = quote_plus(query(phrases, context, days))
    return f"https://news.google.com/rss/search?q={q}&hl=en-KE&gl=KE&ceid=KE:en"


def parse_feed(xml_text: str, match_terms: list[str]) -> list[Headline]:
    root = ET.fromstring(xml_text)
    out: list[Headline] = []
    seen = set()
    for item in root.iter("item"):
        # NFKC folds "𝐀𝐛𝐬𝐚"-style Unicode bold (common on social posts) back to plain letters
        title = unicodedata.normalize("NFKC", html.unescape((item.findtext("title") or "").strip()))
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google appends " - Source" to titles; strip it.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        low = title.lower()
        if match_terms and not any(_contains(low, t) for t in match_terms):
            continue
        key = re.sub(r"\W+", " ", low).strip()
        if key in seen:
            continue
        seen.add(key)
        pub = item.findtext("pubDate")
        try:
            published = email.utils.parsedate_to_datetime(pub) if pub else timezone.now()
        except (TypeError, ValueError):
            published = timezone.now()
        if timezone.is_naive(published):
            published = timezone.make_aware(published, dt.timezone.utc)
        score, label, terms = score_headline(title)
        out.append(
            Headline(
                title=title,
                url=safe_url(item.findtext("link") or ""),
                source=source,
                published_at=published,
                score=score,
                label=label,
                terms=terms,
            )
        )
    return out


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


# Headlines that mention these are almost always about a namesake abroad.
FOREIGN_MARKERS = (" rbi ", " india", " mysuru", " tsxv", " nasdaq", " nyse:", " asx", " lse:", " pakistan", " sri lanka", " philippines")


def _local(items: list[Headline]) -> list[Headline]:
    return [h for h in items if not any(m in f" {h.title.lower()} " for m in FOREIGN_MARKERS)]


def fetch(phrase: str, match_terms: list[str], context: str = "", days: int | None = None) -> list[Headline]:
    return _local(parse_feed(http.fetch(feed_url(phrase, context, days)), match_terms))


def split_feed(xml_text: str, wanted: dict[str, list[str]]) -> tuple[dict[str, list[Headline]], bool]:
    """Assign one combined feed's headlines to each key whose match terms appear in the title.

    Returns (headlines per key, whether the feed hit Google's item cap and may be incomplete)."""
    items = _local(parse_feed(xml_text, []))
    out = {k: [h for h in items if any(_contains(h.title.lower(), t) for t in terms)] for k, terms in wanted.items()}
    return out, xml_text.count("<item>") >= FEED_CAP - 5


def fetch_group(phrases: list[str], wanted: dict[str, list[str]], context: str, days: int):
    return split_feed(http.fetch(feed_url(phrases, context, days)), wanted)
