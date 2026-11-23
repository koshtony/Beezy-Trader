"""Kenyan macro data: Central Bank of Kenya (rates, inflation, shilling) and EIA (Brent crude).

All public pages and files, about nine requests a day:
* CBK homepage            today's USD/KES and the "Key Rates" box (CBR, KESONIA, 91-day T-bill,
                          inflation, lending and deposit rates)
* CBK rate pages          CBR decision history, monthly inflation since 2005
* CBK statistics files    monthly interbank and T-bill rates, bank lending/deposit rates (1991–),
                          end-of-month exchange rates (1993–); file links are read from the
                          statistics pages because their names change with each upload
* EIA                     Brent spot price, daily since 1987 (US government, public domain)
"""

import calendar
import csv
import datetime as dt
import io
import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from django.db import transaction

from ..models import MacroPoint
from . import http

log = logging.getLogger("market.macro")

CBK = "https://www.centralbank.go.ke"
EIA_BRENT = "https://www.eia.gov/dnav/pet/hist/RBRTED.htm"

# series key -> (name, unit, source)
SERIES = {
    "cbr": ("Central Bank Rate", "%", "CBK"),
    "tbill91": ("91-day T-bill", "%", "CBK"),
    "interbank": ("Interbank rate", "%", "CBK"),
    "kesonia": ("KESONIA", "%", "CBK"),
    "inflation": ("Inflation (12-month)", "%", "CBK / KNBS"),
    "lending": ("Bank lending rate", "%", "CBK"),
    "deposit": ("Bank deposit rate", "%", "CBK"),
    "usdkes": ("USD/KES", "KES", "CBK"),
    "brent": ("Brent crude", "USD/bbl", "EIA"),
}

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

Point = tuple[str, dt.date, float]


def month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _month(name: str) -> int | None:
    name = (name or "").strip().lower()
    return MONTHS.get(name) or MONTHS.get(name[:3])


def _dmy(s: str) -> dt.date | None:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- parsers
def parse_home(html: str) -> list[Point]:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    out: list[Point] = []
    posted = re.search(r"Posted On:\s*(\d{2}-\d{2}-\d{4})", text)
    usd = re.search(r"US DOLLAR\s+([\d.,]+)", text)
    if posted and usd and _dmy(posted.group(1)) and _num(usd.group(1)):
        out.append(("usdkes", _dmy(posted.group(1)), _num(usd.group(1))))
    for key, label in (("cbr", "Central Bank Rate"), ("kesonia", "KESONIA"), ("tbill91", "91-Day T-Bill")):
        m = re.search(rf"{re.escape(label)}\s+([\d.]+)%\s+(\d{{2}}/\d{{2}}/\d{{4}})", text)
        if m and _dmy(m.group(2)) and _num(m.group(1)) is not None:
            out.append((key, _dmy(m.group(2)), _num(m.group(1))))
    for key, label in (("inflation", "Inflation Rate"), ("lending", "Lending Rate"), ("deposit", "Deposit Rate")):
        m = re.search(rf"{re.escape(label)}\s+([\d.]+)%\s+([A-Za-z]+),\s*(\d{{4}})", text)
        if m and _month(m.group(2)) and _num(m.group(1)) is not None:
            out.append((key, month_end(int(m.group(3)), _month(m.group(2))), _num(m.group(1))))
    return out


def _rows(html: str, first_table: bool = True) -> list[list[str]]:
    soup = BeautifulSoup(html, "lxml")
    scope = soup.find("table") if first_table else soup
    if scope is None:
        return []
    return [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in scope.find_all("tr")]


def parse_cbr_page(html: str) -> list[Point]:
    """Monetary Policy Committee decisions: Date | Rate."""
    out = []
    for r in _rows(html):
        if len(r) >= 2 and _dmy(r[0]) and _num(r[1]) is not None:
            out.append(("cbr", _dmy(r[0]), _num(r[1])))
    return out


def parse_inflation_page(html: str) -> list[Point]:
    """Year | Month | Annual average | 12-month inflation."""
    out = []
    for r in _rows(html):
        if len(r) >= 4 and r[0].isdigit() and _month(r[1]) and _num(r[3]) is not None:
            out.append(("inflation", month_end(int(r[0]), _month(r[1])), _num(r[3])))
    return out


def _csv_table(text: str, first: str) -> tuple[list[str], list[list[str]]]:
    """Header row starting with `first` (case-insensitive) and the rows after it."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    for i, r in enumerate(rows):
        if r and r[0].strip().lower() == first:
            return [c.strip().lower() for c in r], rows[i + 1:]
    return [], []


def _monthly(text: str, columns: dict[str, str], numeric_month: bool = False) -> list[Point]:
    """CBK monthly files: the year is only written on its first row (January)."""
    header, rows = _csv_table(text, "year")
    if not header:
        return []
    idx = {series: header.index(col) for col, series in columns.items() if col in header}
    out, year = [], None
    for r in rows:
        if len(r) < 2:
            continue
        if r[0].strip().isdigit():
            year = int(r[0])
        month = int(r[1]) if numeric_month and r[1].strip().isdigit() else _month(r[1])
        if year is None or not month:
            continue
        for series, i in idx.items():
            v = _num(r[i]) if i < len(r) else None
            if v is not None:
                out.append((series, month_end(year, month), v))
    return out


def parse_rates_csv(text: str) -> list[Point]:
    return _monthly(text, {"interbank rate": "interbank", "91-day tbill": "tbill91"})


def parse_bank_rates_csv(text: str) -> list[Point]:
    return _monthly(text, {"lending": "lending", "deposit": "deposit"})


def parse_fx_csv(text: str) -> list[Point]:
    return _monthly(text, {"united states dollar": "usdkes"}, numeric_month=True)


def parse_eia_brent(html: str) -> list[Point]:
    """EIA weekly rows: 'Week Of' (e.g. '2026 Nov-16 to Nov-20') then Mon..Fri prices."""
    out = []
    for r in _rows(html, first_table=False):
        m = re.match(r"(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})", r[0]) if r else None
        if not m or len(r) < 6:
            continue
        try:
            monday = dt.date(int(m.group(1)), _month(m.group(2)), int(m.group(3)))
        except (TypeError, ValueError):
            continue
        for k, cell in enumerate(r[1:6]):
            v = _num(cell)
            if v is not None:
                out.append(("brent", monday + dt.timedelta(days=k), v))
    return out


def find_link(html: str, *words: str) -> str | None:
    """Absolute URL of the first .csv link whose name contains all `words`."""
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if low.endswith(".csv") and all(w.lower() in low for w in words):
            return urljoin(CBK, href)
    return None


# ---------------------------------------------------------------- storage
def store(points: list[Point], source: str) -> int:
    if not points:
        return 0
    latest: dict[tuple[str, dt.date], float] = {}
    for series, date, value in points:
        latest[(series, date)] = value
    objs = [MacroPoint(series=s, date=d, value=v, source=source) for (s, d), v in latest.items()]
    with transaction.atomic():
        MacroPoint.objects.bulk_create(
            objs, batch_size=1000, update_conflicts=True, unique_fields=["series", "date"], update_fields=["value", "source"]
        )
    return len(objs)


def update() -> str:
    """Fetch every source. One failing source doesn't stop the others. Today's values go first so the
    more precise monthly files win where both cover the same month (e.g. inflation 7.32 vs 7.3)."""
    from .ingest import job

    with job("macro") as row:
        done, failed = [], []

        def step(name, fn):
            try:
                n = fn()
                done.append(f"{name} {n}")
            except Exception as exc:
                failed.append(name)
                log.warning("macro source %s failed: %s", name, exc)

        def rates_files():
            page = http.fetch(f"{CBK}/statistics/interest-rates/")
            n = 0
            for words, parser in ((("central bank rates",), parse_rates_csv), (("weighted average rates",), parse_bank_rates_csv)):
                url = find_link(page, *words)
                if url:
                    n += store(parser(http.fetch(url)), "CBK statistics")
            return n

        def fx_file():
            url = find_link(http.fetch(f"{CBK}/statistics/exchange-rates/"), "end period")
            return store(parse_fx_csv(http.fetch(url)), "CBK statistics") if url else 0

        step("today", lambda: store(parse_home(http.fetch(f"{CBK}/")), "CBK homepage"))
        step("rates", rates_files)
        step("fx", fx_file)
        step("cbr", lambda: store(parse_cbr_page(http.fetch(f"{CBK}/rates/central-bank-rate/")), "CBK"))
        step("inflation", lambda: store(parse_inflation_page(http.fetch(f"{CBK}/inflation-rates/")), "CBK"))
        step("brent", lambda: store(parse_eia_brent(http.fetch(EIA_BRENT)), "EIA"))
        row.message = "; ".join(done) + (f" · failed: {', '.join(failed)}" if failed else "")
        if not done:
            raise RuntimeError("all macro sources failed")
        return row.message
