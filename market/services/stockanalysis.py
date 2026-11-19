"""Fundamentals scraper for stockanalysis.com (Nairobi listings live under /quote/nase/<T>/).

We read the human-readable statistics page as a flat sequence of text tokens and pick
values that follow known labels. This is resilient to markup/CSS churn.
"""

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from django.conf import settings

from . import http

log = logging.getLogger("market.stockanalysis")

# Tickers that differ between AFX and stockanalysis can be mapped here.
SYMBOL_MAP: dict[str, str] = {}


def _base():
    return settings.BEEZY["STOCKANALYSIS_BASE"]


def parse_value(tok: str | None) -> float | None:
    if tok is None:
        return None
    t = tok.strip().replace(",", "").replace("KES", "").strip()
    if not t or t.lower() in {"n/a", "-", "—", "upgrade"}:
        return None
    pct = t.endswith("%")
    t = t.rstrip("%")
    mult = 1.0
    if t and t[-1] in "KMBT":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[t[-1]]
        t = t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return v  # percentages are returned as plain numbers (e.g. 41.97 for 41.97%)


def _tokens(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript", "svg"]):
        bad.decompose()
    main = soup.find("main") or soup
    toks = [t.strip() for t in main.stripped_strings]
    return [t for t in toks if t]


# label on page -> (field name)
STAT_LABELS = {
    "PE Ratio": "pe",
    "Forward PE": "forward_pe",
    "PS Ratio": "ps",
    "PB Ratio": "pb",
    "PEG Ratio": "peg",
    "Debt / Equity": "debt_to_equity",
    "Current Ratio": "current_ratio",
    "Return on Equity (ROE)": "roe",
    "Return on Assets (ROA)": "roa",
    "Revenue": "revenue",
    "Gross Profit": "gross_profit",
    "Net Income": "net_income",
    "Earnings Per Share (EPS)": "eps",
    "Cash & Cash Equivalents": "cash",
    "Total Debt": "total_debt",
    "Equity (Book Value)": "equity",
    "Book Value Per Share": "bvps",
    "Operating Cash Flow": "ocf",
    "Capital Expenditures": "capex",
    "Free Cash Flow": "fcf",
    "Gross Margin": "gross_margin",
    "Profit Margin": "profit_margin",
    "Dividend Per Share": "dps",
    "Payout Ratio": "payout_ratio",
    "Shares Outstanding": "shares",
    "Price Target": "price_target",
    "Analyst Consensus": "consensus",
    "Analyst Count": "analyst_count",
    "EPS Growth Forecast (3Y)": "eps_growth_forecast",
    "Revenue Growth Forecast (3Y)": "revenue_growth_forecast",
}
TEXT_FIELDS = {"consensus"}


@dataclass
class SAStats:
    values: dict = field(default_factory=dict)
    eps_history: list = field(default_factory=list)
    fiscal_period: str = ""

    def get(self, k):
        return self.values.get(k)


def parse_statistics(html: str) -> dict:
    toks = _tokens(html)
    out: dict = {}
    for i, tok in enumerate(toks):
        key = STAT_LABELS.get(tok)
        if not key or key in out:
            continue
        # value is the next token that isn't another label
        for j in range(i + 1, min(i + 3, len(toks))):
            nxt = toks[j]
            if nxt in STAT_LABELS:
                break
            if key in TEXT_FIELDS:
                if nxt.lower() not in {"n/a", "upgrade"}:
                    out[key] = nxt
                break
            v = parse_value(nxt)
            if v is not None or nxt.lower() == "n/a":
                out[key] = v
                break
    return out


def parse_financials(html: str) -> tuple[list[dict], str]:
    """Return EPS history (oldest→newest, annual + TTM) from the income statement table."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return [], ""
    rows = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return [], ""
    header = rows[0]
    eps_row = next((r for r in rows if re.match(r"^(Earnings Per Share|EPS \(Basic\))", r[0])), None)
    period_row = next((r for r in rows if r[0].startswith("Period Ending")), None)
    if eps_row is None:
        return [], ""
    hist = []
    for col, label in enumerate(header[1:], start=1):
        if col >= len(eps_row):
            break
        v = parse_value(eps_row[col])
        if v is None:
            continue
        hist.append({"period": label, "eps": v})
    fiscal = ""
    if period_row and len(period_row) > 1:
        fiscal = period_row[1]
    # stockanalysis lists newest first; we store oldest first
    hist.reverse()
    return hist, fiscal


def fetch(ticker: str) -> SAStats:
    sym = SYMBOL_MAP.get(ticker, ticker)
    base = f"{_base()}/quote/nase/{sym}"
    stats_html = http.fetch(f"{base}/statistics/", browser_ua=True)
    values = parse_statistics(stats_html)
    hist, fiscal = [], ""
    try:
        fin_html = http.fetch(f"{base}/financials/", browser_ua=True)
        hist, fiscal = parse_financials(fin_html)
    except Exception as exc:  # pragma: no cover - network dependent
        log.warning("financials fetch failed for %s: %s", ticker, exc)
    return SAStats(values=values, eps_history=hist, fiscal_period=fiscal)
