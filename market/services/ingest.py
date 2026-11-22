"""Persist scraped data into the database."""

import datetime as dt
import logging
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Count
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
    while as_of.weekday() >= 5:  # the NSE never trades at weekends
        as_of -= dt.timedelta(days=1)
    as_of = _session_date(snap, as_of)
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
            idx = index_stock()
            prev = PriceBar.objects.filter(stock=idx, date__lt=as_of).order_by("-date").first()
            # AFX refreshes NASI only after the close; mid-session it still shows the last
            # session's value and change, which must not become a new trading day.
            stale = prev is not None and prev.close == snap.nasi and (prev.change or 0) == (snap.nasi_change or 0)
            if not stale:
                PriceBar.objects.update_or_create(
                    stock=idx,
                    date=as_of,
                    defaults={"close": snap.nasi, "change": snap.nasi_change, "source": "afx-daily"},
                )
    return written


def _session_date(snap: afx.MarketSnapshot, as_of: dt.date) -> dt.date:
    """If the list is just the previous session repeated, keep it on that session's date."""
    prev_date = (
        PriceBar.objects.filter(source="afx-daily", date__lt=as_of)
        .exclude(stock__ticker=INDEX_TICKER)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    if prev_date is None:
        return as_of
    prev = {
        t: (c, ch)
        for t, c, ch in PriceBar.objects.filter(date=prev_date, source="afx-daily").values_list("stock__ticker", "close", "change")
    }
    movers = [r for r in snap.rows if r.price is not None and (prev.get(r.ticker, (None, 0))[1] or 0) != 0]
    if len(movers) < 5:
        return as_of
    same = sum(1 for r in movers if prev[r.ticker] == (r.price, r.change or 0.0))
    return prev_date if same / len(movers) >= 0.9 else as_of


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
FULL_NEWS_EVERY = dt.timedelta(days=7)  # full per-stock sweep (60-day window) at least weekly
QUICK_MIN_DAYS, QUICK_MAX_DAYS = 2, 14  # routine runs only ask for the days since the last run
SOLO_HEADLINES = 8  # stocks with this many headlines in 14 days get a query of their own
GROUP_MAX, GROUP_WORDS = 8, 28  # names per combined query; Google reads at most 32 words


@dataclass
class _NewsTarget:
    stock: Stock
    phrase: str
    terms: list[str]
    context: str


def _news_targets(tickers: list[str] | None) -> list[_NewsTarget]:
    qs = Stock.objects.filter(kind=Stock.KIND_EQUITY, is_suspended=False).exclude(ticker__in=catalog.NON_EQUITY)
    if tickers:
        qs = qs.filter(ticker__in=[t.upper() for t in tickers])
    out = []
    for stock in qs.order_by("ticker"):
        phrase, terms = catalog.news_terms(stock.ticker, stock.name)
        if stock.news_query:
            phrase = stock.news_query
            terms = [stock.news_query.lower(), *terms]
        out.append(_NewsTarget(stock, phrase, terms, catalog.news_context(stock.ticker)))
    return out


def _store_headlines(stock: Stock, items: list[news.Headline]) -> int:
    if not items:
        return 0
    existing = set(NewsArticle.objects.filter(stock=stock, title__in=[it.title[:500] for it in items]).values_list("title", flat=True))
    fresh = [it for it in items if it.title[:500] not in existing]
    NewsArticle.objects.bulk_create(
        [
            NewsArticle(
                stock=stock, title=it.title[:500], url=it.url[:1000], source=it.source[:200],
                published_at=it.published_at, sentiment=it.score, label=it.label, matched_terms=it.terms,
            )
            for it in fresh
        ],
        ignore_conflicts=True,
    )
    return len(fresh)


def news_plan(now=None) -> tuple[bool, int]:
    """(full sweep?, look-back days) for the next automatic news run."""
    now = now or timezone.now()
    ok = JobLog.objects.filter(status=JobLog.OK)
    last_full = ok.filter(job="news-full").values_list("started_at", flat=True).first()
    last_any = ok.filter(job__in=["news", "news-full"]).values_list("started_at", flat=True).first()
    if last_full is None or now - last_full > FULL_NEWS_EVERY or last_any is None:
        return True, settings.BEEZY["NEWS_LOOKBACK_DAYS"]
    gap_days = (now - last_any).total_seconds() / 86400
    days = max(QUICK_MIN_DAYS, int(gap_days) + 2)
    if days > QUICK_MAX_DAYS:
        return True, settings.BEEZY["NEWS_LOOKBACK_DAYS"]
    return False, days


def group_targets(targets: list[_NewsTarget], solo_ids: set[int]) -> list[list[_NewsTarget]]:
    """Busy names alone; quiet ones packed into combined OR queries that share a context word."""
    groups = [[t] for t in targets if t.stock.id in solo_ids]
    by_context: dict[str, list[_NewsTarget]] = {}
    for t in targets:
        if t.stock.id not in solo_ids:
            by_context.setdefault(t.context, []).append(t)
    for context, items in by_context.items():
        cur: list[_NewsTarget] = []
        for t in items:
            words = news.query_words([x.phrase for x in cur + [t]], context)
            if cur and (len(cur) >= GROUP_MAX or words > GROUP_WORDS):
                groups.append(cur)
                cur = []
            cur.append(t)
        if cur:
            groups.append(cur)
    return groups


def update_news(tickers: list[str] | None = None, *, full: bool | None = None) -> int:
    """Fetch headlines. A full sweep queries each stock over 60 days; routine runs batch quiet
    stocks into a few combined queries covering only the days since the last run."""
    targets = _news_targets(tickers)
    auto_full, days = news_plan()
    if full is None:
        full = bool(tickers) or auto_full
    if full:
        days = settings.BEEZY["NEWS_LOOKBACK_DAYS"]
    added, failed = 0, 0
    with job("news-full" if full else "news") as row:
        if full:
            groups = [[t] for t in targets]
        else:
            since = timezone.now() - dt.timedelta(days=14)
            busy = (
                NewsArticle.objects.filter(published_at__gte=since)
                .values("stock").annotate(n=Count("id")).filter(n__gte=SOLO_HEADLINES)
                .values_list("stock", flat=True)
            )
            groups = group_targets(targets, set(busy))
        queue, done = list(groups), 0
        while queue:
            grp = queue.pop(0)
            done += 1
            _progress(row, f"{done}/{done + len(queue)} {grp[0].stock.ticker}{f' +{len(grp) - 1}' if len(grp) > 1 else ''}")
            try:
                if len(grp) == 1:
                    t = grp[0]
                    found = {t.stock.ticker: news.fetch(t.phrase, t.terms, t.context, days)}
                else:
                    found, capped = news.fetch_group(
                        [t.phrase for t in grp], {t.stock.ticker: t.terms for t in grp}, grp[0].context, days
                    )
                    if capped:  # feed hit Google's item cap: some headlines may be missing, split it
                        mid = len(grp) // 2
                        queue[:0] = [grp[:mid], grp[mid:]]
                        continue
            except Exception as exc:
                failed += len(grp)
                log.warning("news failed for %s: %s", ", ".join(t.stock.ticker for t in grp), exc)
                continue
            for t in grp:
                added += _store_headlines(t.stock, found.get(t.stock.ticker, []))
        mode = "full sweep" if full else f"last {days} days"
        row.message = f"{added} new headlines for {len(targets)} stocks · {done} queries · {mode}" + (
            f" · {failed} failed" if failed else ""
        )
    return added
