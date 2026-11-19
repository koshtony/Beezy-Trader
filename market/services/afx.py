"""Scraper for afx.kwayisi.org — NSE daily price list, per-stock profile and price history.

robots.txt on that host asks for a 60 second crawl delay; ``http.fetch`` enforces it.
"""

import datetime as dt
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from django.conf import settings

from . import http

log = logging.getLogger("market.afx")


def _base():
    return settings.BEEZY["AFX_BASE"]


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("+", "")
    if not t or t in {"-", "—", "n/a"}:
        return None
    mult = 1.0
    if t[-1:] in "KMBT" and len(t) > 1:
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[t[-1]]
        t = t[:-1]
    t = t.rstrip("%")
    try:
        return float(t) * mult
    except ValueError:
        return None


@dataclass
class ListingRow:
    ticker: str
    name: str
    volume: int | None
    price: float | None
    change: float | None
    suspended: bool


@dataclass
class MarketSnapshot:
    as_of: dt.date | None
    nasi: float | None
    nasi_change: float | None
    market_cap: float | None
    rows: list[ListingRow]


def parse_listing(html: str) -> MarketSnapshot:
    soup = BeautifulSoup(html, "lxml")
    rows: list[ListingRow] = []
    target = None
    for table in soup.find_all("table"):
        heads = [th.get_text(strip=True) for th in table.find_all("th")]
        if heads[:5] == ["Ticker", "Name", "Volume", "Price", "Change"]:
            target = table
            break
    if target is None:
        raise ValueError("NSE price table not found on AFX listing page")
    for tr in target.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        ticker = tds[0].get_text(strip=True).upper()
        vol = _num(tds[2].get_text())
        rows.append(
            ListingRow(
                ticker=ticker,
                name=tds[1].get_text(strip=True),
                volume=int(vol) if vol is not None else None,
                price=_num(tds[3].get_text()),
                change=_num(tds[4].get_text()),
                suspended="ss" in (tr.get("class") or []),
            )
        )

    nasi = nasi_change = mcap = None
    first = soup.find("table")
    if first is not None:
        tds = first.find_all("td")
        if len(tds) >= 3:
            m = re.match(r"\s*([\d,.]+)\s*\(([-+\d.,]+)\)", tds[0].get_text(" ", strip=True))
            if m:
                nasi, nasi_change = _num(m.group(1)), _num(m.group(2))
            cap = tds[2].get_text(strip=True).replace("KES", "").strip()
            mcap = _num(cap.replace("Tr", "T").replace("Bn", "B"))

    as_of = None
    m = re.search(r"<time[^>]*datetime=\"?(\d{4}-\d{2}-\d{2})", html)
    if m:
        as_of = dt.date.fromisoformat(m.group(1))
    return MarketSnapshot(as_of=as_of, nasi=nasi, nasi_change=nasi_change, market_cap=mcap, rows=rows)


def fetch_listing() -> MarketSnapshot:
    return parse_listing(http.fetch(f"{_base()}/nse/"))


_CHART_POINT = re.compile(r'\[d\("(\d{4}-\d{2}-\d{2})"\),\s*(-?[\d.]+)\]')


def parse_chart(js: str) -> list[tuple[dt.date, float]]:
    """Parse the Highstock series served by /chart/nse/<ticker> into (date, close) pairs."""
    out = []
    for d, v in _CHART_POINT.findall(js):
        out.append((dt.date.fromisoformat(d), float(v)))
    out.sort()
    return out


def fetch_index_history() -> list[tuple[dt.date, float]]:
    """NASI daily closes (the chart on the exchange's front page)."""
    return parse_chart(http.fetch(f"{_base()}/chart/nse", headers={"Referer": f"{_base()}/nse/"}))


def fetch_history(ticker: str) -> list[tuple[dt.date, float]]:
    slug = ticker.lower()
    js = http.fetch(
        f"{_base()}/chart/nse/{slug}", headers={"Referer": f"{_base()}/nse/{slug}.html"}
    )
    return parse_chart(js)


@dataclass
class Profile:
    sector: str = ""
    industry: str = ""
    eps: float | None = None
    pe: float | None = None
    dps: float | None = None
    dividend_yield: float | None = None
    shares: float | None = None
    market_cap: float | None = None
    recent: list[tuple[dt.date, int | None, float]] | None = None  # (date, volume, close)


def parse_profile(html: str) -> Profile:
    soup = BeautifulSoup(html, "lxml")
    p = Profile(recent=[])
    for dt_tag in soup.select("[data-fact] dt"):
        label = dt_tag.get_text(strip=True).lower()
        dd = dt_tag.find_next_sibling("dd")
        if not dd:
            continue
        if label == "sector":
            p.sector = dd.get_text(strip=True)
        elif label == "industry":
            p.industry = dd.get_text(strip=True)
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        k, v = tds[0].get_text(strip=True).lower(), tds[1].get_text(strip=True)
        if k == "earnings per share":
            p.eps = _num(v)
        elif k == "price/earning ratio":
            p.pe = _num(v)
        elif k == "dividend per share":
            p.dps = _num(v)
        elif k == "dividend yield":
            p.dividend_yield = _num(v)
        elif k == "shares outstanding":
            p.shares = _num(v)
        elif k == "market capitalization":
            p.market_cap = _num(v)
    hist = soup.select_one("table[data-hist]")
    if hist:
        for tr in hist.select("tbody tr"):
            tds = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(tds) >= 3:
                vol = _num(tds[1])
                p.recent.append(
                    (dt.date.fromisoformat(tds[0]), int(vol) if vol else None, _num(tds[2]))
                )
    return p


def fetch_profile(ticker: str) -> Profile:
    return parse_profile(http.fetch(f"{_base()}/nse/{ticker.lower()}.html"))
