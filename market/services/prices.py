"""Daily NSE price list from several independent sources, tried in turn.

    afx                 afx.kwayisi.org/nse/                       primary; NASI; 60 s crawl delay
    mansa               mansamarkets.com/kenya                     end-of-day board, ticker + volume
    africanfinancials   africanfinancials.com (NSE share prices)   per-row trade dates, exact volumes
    rokam               stocks.rokamsystem.co.ke                   close/prev/volume and NASI as data attributes

Every source is parsed into the same `afx.MarketSnapshot`. A snapshot is used only if it passes
sanity checks (enough counters, a recent date, prices in line with the stored closes); otherwise the
next source is tried. A source that fails is moved to the back of the queue for six hours, so an
unreachable host (e.g. AFX from some cloud servers) doesn't slow every run. All four pages allow
crawling in their robots.txt; each is fetched once per refresh.
"""

import datetime as dt
import logging
import re
from collections import Counter
from statistics import median

from bs4 import BeautifulSoup
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..models import PriceBar
from . import afx, http
from .afx import ListingRow, MarketSnapshot, _num

log = logging.getLogger("market.prices")

NAMES = {"afx": "AFX", "mansa": "Mansa Markets", "africanfinancials": "African Financials", "rokam": "Rokam"}
URLS = {
    "mansa": "https://www.mansamarkets.com/kenya",
    "africanfinancials": "https://africanfinancials.com/nairobi-securities-exchange-kenya-share-prices/",
    "rokam": "https://stocks.rokamsystem.co.ke/",
}
# Other sites' codes for the same counters
TICKER_ALIASES = {"HFCK": "HFCB", "AMA": "AMAC", "KAPCHORUA": "KAPC"}
MIN_ROWS = 40
MAX_AGE_DAYS = 5
DEMOTE_FOR = 6 * 3600
BROWSER_UA = True  # these sites serve the full table to ordinary browsers


class SourceRejected(Exception):
    pass


def _ticker(code: str) -> str:
    code = (code or "").strip().upper().removesuffix(".KE").lstrip("^")
    return TICKER_ALIASES.get(code, code)


def _year_for(month: int, day: int, today: dt.date) -> dt.date:
    """A day and month with no year: this year's, unless that is in the future (then last year's)."""
    d = dt.date(today.year, month, day)
    return d.replace(year=today.year - 1) if d > today + dt.timedelta(days=1) else d


# ---------------------------------------------------------------- parsers
def parse_mansa(html: str, today: dt.date | None = None) -> MarketSnapshot:
    today = today or timezone.localdate()
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        link = tds[1].find("a")
        code = tds[1].find("span")
        if link is None or code is None:
            continue
        price = _num(tds[2].get_text(strip=True).replace("KSh", ""))
        change = _num(tds[3].get_text(strip=True).replace("−", "-"))
        vol = _num(tds[5].get_text(strip=True))
        rows.append(ListingRow(ticker=_ticker(code.get_text(strip=True)), name=link.get_text(strip=True),
                               volume=int(vol) if vol is not None else None, price=price, change=change, suspended=False))
    as_of = None
    m = re.search(r"Last updated\s+(\d{1,2})\s+([A-Za-z]{3})", html)
    if m:
        month = dt.datetime.strptime(m.group(2), "%b").month
        as_of = _year_for(month, int(m.group(1)), today)
    return MarketSnapshot(as_of=as_of, nasi=None, nasi_change=None, market_cap=None, rows=rows)


def parse_africanfinancials(html: str) -> MarketSnapshot:
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        heads = [th.get_text(strip=True) for th in t.find_all("th")]
        if heads[:2] == ["Company", "Price"] and "Updated" in heads:
            table = t
            break
    if table is None:
        raise SourceRejected("price table not found")
    parsed = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        link = tds[0].find("a")
        m = re.search(r"\(([A-Z0-9.\-]+)\.ke\)", (link.get("title") if link else "") or "", re.I)
        if not m:
            continue
        try:
            traded = dt.datetime.strptime(tds[7].get_text(strip=True), "%b %d, %Y").date()
        except ValueError:
            traded = None
        price = _num(tds[1].get_text(strip=True))
        pct = _num(tds[2].get_text(strip=True))
        vol = _num(tds[4].get_text(strip=True))
        name = (link.get("title") or "").rsplit("(", 1)[0].strip() or link.get_text(strip=True)
        parsed.append((_ticker(m.group(1)), name, price, pct, vol, traded))
    dates = Counter(p[5] for p in parsed if p[5])
    as_of = dates.most_common(1)[0][0] if dates else None
    rows = []
    for ticker, name, price, pct, vol, traded in parsed:
        moved_today = traded == as_of
        change = None
        if price is not None and pct is not None and moved_today and pct > -100:
            change = round(price - price / (1 + pct / 100), 4)  # the page gives % only
        rows.append(ListingRow(ticker=ticker, name=name, volume=int(vol) if vol is not None and moved_today else 0,
                               price=price, change=change if moved_today else 0.0, suspended=False))
    return MarketSnapshot(as_of=as_of, nasi=None, nasi_change=None, market_cap=None, rows=rows)


def parse_rokam(html: str) -> MarketSnapshot:
    soup = BeautifulSoup(html, "lxml")
    rows, nasi, nasi_change = [], None, None
    for tr in soup.select("tr[data-code]"):
        code, price = tr.get("data-code", ""), _num(tr.get("data-price"))
        change = _num(tr.get("data-change-amt"))
        if tr.get("data-is-index") == "1":
            if code.upper() == "^NASI":
                nasi, nasi_change = price, change
            continue
        tds = tr.find_all("td")
        vol = _num(tds[14].get_text(strip=True)) if len(tds) > 14 else None
        rows.append(ListingRow(ticker=_ticker(code), name=tr.get("data-name-display") or tr.get("data-name") or code,
                               volume=int(vol) if vol is not None else None, price=price, change=change, suspended=False))
    m = re.search(r"Market Price List \((\d{4}-\d{2}-\d{2})\)", html)
    as_of = dt.date.fromisoformat(m.group(1)) if m else None
    return MarketSnapshot(as_of=as_of, nasi=nasi, nasi_change=nasi_change, market_cap=None, rows=rows)


PARSERS = {"mansa": parse_mansa, "africanfinancials": parse_africanfinancials, "rokam": parse_rokam}


def fetch_source(name: str, retries: int = 1) -> MarketSnapshot:
    if name == "afx":
        return afx.parse_listing(http.fetch(f"{settings.BEEZY['AFX_BASE']}/nse/", retries=retries))
    return PARSERS[name](http.fetch(URLS[name], retries=retries, browser_ua=BROWSER_UA))


# ---------------------------------------------------------------- checks and failover
def check(snap: MarketSnapshot, today: dt.date | None = None) -> None:
    """Raise SourceRejected unless the snapshot looks like a real, current NSE price list."""
    today = today or timezone.localdate()
    priced = {r.ticker: r.price for r in snap.rows if r.price and r.price > 0}
    if len(priced) < MIN_ROWS:
        raise SourceRejected(f"only {len(priced)} prices")
    if snap.as_of is None:
        raise SourceRejected("no date on the price list")
    if (today - snap.as_of).days > MAX_AGE_DAYS or snap.as_of > today + dt.timedelta(days=1):
        raise SourceRejected(f"price list is dated {snap.as_of}")
    recent = {}
    for t, c in (PriceBar.objects.filter(date__gte=snap.as_of - dt.timedelta(days=14), date__lte=snap.as_of)
                 .order_by("date").values_list("stock__ticker", "close")):
        recent[t] = c  # latest wins
    ratios = [priced[t] / c - 1 for t, c in recent.items() if t in priced and c]
    if len(ratios) >= 20:
        if median(abs(x) for x in ratios) > 0.10 or sum(abs(x) > 0.5 for x in ratios) > len(ratios) * 0.1:
            raise SourceRejected("prices don't match recent closes (wrong page or units?)")


def is_down(name: str) -> bool:
    return bool(cache.get(f"bz:price-down:{name}"))


def order() -> list[str]:
    names = [n.strip() for n in settings.BEEZY["PRICE_SOURCES"].split(",") if n.strip() in NAMES]
    healthy = [n for n in names if not cache.get(f"bz:price-down:{n}")]
    return healthy + [n for n in names if n not in healthy]


def fetch_snapshot() -> tuple[MarketSnapshot, str, list[str]]:
    """The first source whose price list passes the checks: (snapshot, source key, problems with earlier ones)."""
    from .pipeline import short_error

    problems = []
    for name in order():
        try:
            snap = fetch_source(name, retries=0)  # no retry: the next source is the retry
            check(snap)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SourceRejected) else short_error(str(exc))
            problems.append(f"{NAMES[name]}: {reason}")
            cache.set(f"bz:price-down:{name}", reason, DEMOTE_FOR)
            log.warning("price source %s failed: %s", name, reason)
            continue
        cache.delete(f"bz:price-down:{name}")
        return snap, name, problems
    raise RuntimeError("all price sources failed: " + "; ".join(problems))
