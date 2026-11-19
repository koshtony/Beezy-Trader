"""Persist scraped data into the database."""

import datetime as dt
import logging
from contextlib import contextmanager

from django.db import transaction
from django.utils import timezone

from ..models import Fundamentals, JobLog, NewsArticle, PriceBar, Stock
from . import afx, catalog, news, stockanalysis

log = logging.getLogger("market.ingest")

INDEX_TICKER = "NASI"


@contextmanager
def job(name: str):
    """Record a job run in JobLog; yields the log row so callers can report progress."""
    row = JobLog.objects.create(job=name)
    try:
        yield row
    except BaseException as exc:  # includes Ctrl+C, so a killed job never looks "running" forever
        row.status, row.message = JobLog.FAILED, f"{type(exc).__name__}: {exc}"
        row.finished_at = timezone.now()
        row.save()
        raise
    else:
        row.status = JobLog.OK
        row.finished_at = timezone.now()
        row.save()


def _progress(row: JobLog | None, text: str):
    if row is not None:
        row.progress = text[:120]
        row.save(update_fields=["progress"])
    log.info(text)


def ensure_stock(ticker: str, name: str, *, suspended: bool | None = None) -> Stock:
    kind = Stock.KIND_EQUITY
    stock, created = Stock.objects.get_or_create(
        ticker=ticker,
        defaults={"name": name, "kind": kind, "sector": catalog.sector_for(ticker)},
    )
    dirty = []
    if name and stock.name != name:
        stock.name = name
        dirty.append("name")
    if not stock.sector or stock.sector == "Other":
        sec = catalog.sector_for(ticker)
        if sec != stock.sector:
            stock.sector = sec
            dirty.append("sector")
    if suspended is not None and stock.is_suspended != suspended:
        stock.is_suspended = suspended
        dirty.append("is_suspended")
    if dirty:
        stock.save(update_fields=dirty + ["updated_at"])
    return stock


def index_stock() -> Stock:
    stock, _ = Stock.objects.get_or_create(
        ticker=INDEX_TICKER,
        defaults={"name": "NSE All Share Index", "kind": Stock.KIND_INDEX, "sector": "Index"},
    )
    return stock


# ---------------------------------------------------------------- prices
def store_snapshot(snap: afx.MarketSnapshot) -> int:
    """Upsert the daily price list. Returns the number of bars written."""
    as_of = snap.as_of or timezone.localdate()
    written = 0
    with transaction.atomic():
        for row in snap.rows:
            stock = ensure_stock(row.ticker, row.name, suspended=row.suspended)
            if row.price is None or row.suspended:
                continue
            PriceBar.objects.update_or_create(
                stock=stock,
                date=as_of,
                defaults={
                    "close": row.price,
                    "volume": row.volume or 0,
                    "change": row.change or 0.0,
                    "source": "afx-daily",
                },
            )
            written += 1
        if snap.nasi:
            PriceBar.objects.update_or_create(
                stock=index_stock(),
                date=as_of,
                defaults={"close": snap.nasi, "change": snap.nasi_change, "source": "afx-daily"},
            )
    return written


def update_daily_prices() -> int:
    with job("prices") as row:
        snap = afx.fetch_listing()
        n = store_snapshot(snap)
        row.message = f"{n} closes for {snap.as_of} (NASI {snap.nasi})"
        return n


def store_history(stock: Stock, points: list[tuple[dt.date, float]]) -> int:
    if not points:
        return 0
    bars = [PriceBar(stock=stock, date=d, close=c, source="afx-hist") for d, c in points]
    with transaction.atomic():
        PriceBar.objects.bulk_create(
            bars,
            batch_size=1000,
            update_conflicts=True,
            unique_fields=["stock", "date"],
            update_fields=["close"],
        )
        stock.history_backfilled_at = timezone.now()
        stock.save(update_fields=["history_backfilled_at"])
    return len(bars)


def backfill_history(tickers: list[str] | None = None, *, only_missing: bool = False) -> int:
    qs = Stock.objects.filter(kind=Stock.KIND_EQUITY, is_suspended=False)
    if tickers:
        qs = qs.filter(ticker__in=[t.upper() for t in tickers])
    if only_missing:
        qs = qs.filter(history_backfilled_at__isnull=True)
    stocks = list(qs.order_by("ticker"))
    total = 0
    with job("history") as row:
        if not tickers and (not only_missing or index_stock().history_backfilled_at is None):
            try:
                total += store_history(index_stock(), afx.fetch_index_history())
            except Exception as exc:  # index chart is a nice-to-have (beta)
                log.warning("NASI history unavailable: %s", exc)
        for i, stock in enumerate(stocks, 1):
            _progress(row, f"{i}/{len(stocks)} {stock.ticker}")
            try:
                n = store_history(stock, afx.fetch_history(stock.ticker))
                total += n
                log.info("%s: %d historical closes", stock.ticker, n)
            except Exception as exc:
                log.warning("history failed for %s: %s", stock.ticker, exc)
        row.message = f"{total} historical closes for {len(stocks)} stocks"
    return total


# ---------------------------------------------------------------- fundamentals
def _clean_period(p: str) -> str:
    # "Sep '26 Sep 30, 2026" -> "Sep 30, 2026"
    parts = p.split(" ")
    return " ".join(parts[2:]) if len(parts) > 3 else p


def store_fundamentals(stock: Stock, s: stockanalysis.SAStats) -> Fundamentals:
    v = s.values
    capex = v.get("capex")
    f, _ = Fundamentals.objects.get_or_create(stock=stock)
    f.eps_ttm = v.get("eps")
    f.eps_history = s.eps_history
    f.revenue_ttm = v.get("revenue")
    f.gross_profit_ttm = v.get("gross_profit")
    f.net_income_ttm = v.get("net_income")
    f.equity = v.get("equity")
    f.book_value_per_share = v.get("bvps")
    f.total_debt = v.get("total_debt")
    f.cash = v.get("cash")
    f.operating_cash_flow = v.get("ocf")
    f.capex = abs(capex) if capex is not None else None
    f.free_cash_flow = v.get("fcf")
    f.dividend_per_share = v.get("dps")
    f.shares_outstanding = v.get("shares")
    f.src_pe = v.get("pe")
    f.src_forward_pe = v.get("forward_pe")
    f.src_peg = v.get("peg")
    f.src_pb = v.get("pb")
    f.src_ps = v.get("ps")
    f.src_roe = v.get("roe")
    f.src_roa = v.get("roa")
    f.src_debt_to_equity = v.get("debt_to_equity")
    f.src_gross_margin = v.get("gross_margin")
    f.src_profit_margin = v.get("profit_margin")
    f.src_payout_ratio = v.get("payout_ratio")
    f.src_current_ratio = v.get("current_ratio")
    f.eps_growth_forecast = v.get("eps_growth_forecast")
    f.revenue_growth_forecast = v.get("revenue_growth_forecast")
    f.analyst_target = v.get("price_target")
    f.analyst_consensus = v.get("consensus") or ""
    ac = v.get("analyst_count")
    f.analyst_count = int(ac) if ac else None
    f.fiscal_period = _clean_period(s.fiscal_period)
    f.source = "stockanalysis.com"
    f.save()
    return f


def update_fundamentals(tickers: list[str] | None = None) -> int:
    qs = Stock.objects.filter(kind=Stock.KIND_EQUITY).exclude(ticker__in=catalog.NON_EQUITY)
    if tickers:
        qs = qs.filter(ticker__in=[t.upper() for t in tickers])
    stocks = list(qs.order_by("ticker"))
    ok = 0
    with job("fundamentals") as row:
        for i, stock in enumerate(stocks, 1):
            _progress(row, f"{i}/{len(stocks)} {stock.ticker}")
            try:
                stats = stockanalysis.fetch(stock.ticker)
                if not stats.values:
                    log.info("no fundamentals for %s", stock.ticker)
                    continue
                store_fundamentals(stock, stats)
                ok += 1
            except Exception as exc:
                log.warning("fundamentals failed for %s: %s", stock.ticker, exc)
        row.message = f"fundamentals for {ok}/{len(stocks)} stocks"
    return ok


# ---------------------------------------------------------------- news
def update_news(tickers: list[str] | None = None) -> int:
    qs = Stock.objects.filter(kind=Stock.KIND_EQUITY, is_suspended=False).exclude(
        ticker__in=catalog.NON_EQUITY
    )
    if tickers:
        qs = qs.filter(ticker__in=[t.upper() for t in tickers])
    stocks = list(qs.order_by("ticker"))
    added = 0
    with job("news") as row:
        for i, stock in enumerate(stocks, 1):
            _progress(row, f"{i}/{len(stocks)} {stock.ticker}")
            phrase, terms = catalog.news_terms(stock.ticker, stock.name)
            if stock.news_query:
                phrase = stock.news_query
                terms = [stock.news_query.lower(), *terms]
            try:
                items = news.fetch(phrase, terms, catalog.news_context(stock.ticker))
            except Exception as exc:
                log.warning("news failed for %s: %s", stock.ticker, exc)
                continue
            for it in items:
                _, created = NewsArticle.objects.update_or_create(
                    stock=stock,
                    title=it.title[:500],
                    defaults={
                        "url": it.url[:1000],
                        "source": it.source[:200],
                        "published_at": it.published_at,
                        "sentiment": it.score,
                        "label": it.label,
                        "matched_terms": it.terms,
                    },
                )
                added += int(created)
        row.message = f"{added} new headlines for {len(stocks)} stocks"
    return added
