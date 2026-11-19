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


def feed_url(phrase: str, context: str = "") -> str:
    days = settings.BEEZY["NEWS_LOOKBACK_DAYS"]
    q = quote_plus(f'"{phrase}" {context} when:{days}d'.replace("  ", " "))
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


def fetch(phrase: str, match_terms: list[str], context: str = "") -> list[Headline]:
    items = parse_feed(http.fetch(feed_url(phrase, context)), match_terms)
    return [h for h in items if not any(m in f" {h.title.lower()} " for m in FOREIGN_MARKERS)]
