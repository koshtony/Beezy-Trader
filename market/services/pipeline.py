"""Analysis pipeline: rebuild analytics, projections and recommendations from stored data."""

import datetime as dt
import logging
import threading
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection, transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from ..models import (
    Fundamentals,
    JobLog,
    NewsArticle,
    PriceBar,
    Projection,
    RecommendationRun,
    Stock,
    StockAnalytics,
)
from . import catalog, ingest, locks, macro, metrics, projections, scoring

log = logging.getLogger("market.pipeline")


def _series_by_stock() -> dict[int, metrics.Series]:
    rows = defaultdict(list)
    for sid, d, c, v in PriceBar.objects.order_by("stock_id", "date").values_list(
        "stock_id", "date", "close", "volume"
    ):
        rows[sid].append((d, c, v))
    return {sid: metrics.make_series(r) for sid, r in rows.items()}


def _reported_changes() -> dict[int, tuple[dt.date, float]]:
    """Latest exchange-reported daily change per stock (from the daily price list)."""
    out: dict[int, tuple[dt.date, float]] = {}
    for sid, d, ch in (
        PriceBar.objects.filter(source="afx-daily", change__isnull=False)
        .order_by("stock_id", "date")
        .values_list("stock_id", "date", "change")
    ):
        out[sid] = (d, ch)
    return out


def _apply_reported_change(tech: dict, s, reported) -> None:
    """Prefer the exchange's own day change over one derived from (possibly gappy) history."""
    if not reported or not tech:
        return
    d, ch = reported
    if str(d) != str(s.dates[-1]):
        return
    last = tech["last"]
    prev = last - ch
    if prev > 0:
        tech["change"], tech["prev_close"] = round(ch, 6), round(prev, 6)
        tech["change_pct"] = round(ch / prev * 100, 6)


def run_analysis(with_backtest: bool = True) -> RecommendationRun:
    """Rebuild analytics. Backtests are reused when a stock's last price date hasn't changed;
    with_backtest=False never computes new ones (quick news-only refresh)."""
    with ingest.job("analysis") as jl:
        run = _run_analysis(jl, with_backtest)
        jl.message = f"{len(run.payload.get('universe', []))} stocks analysed"
    cache.clear()
    return run


def _run_analysis(jl: JobLog, with_backtest: bool) -> RecommendationRun:
    now = timezone.now()
    series = _series_by_stock()
    index = Stock.objects.filter(ticker=ingest.INDEX_TICKER).first()
    index_series = series.get(index.id) if index else None
    reported = _reported_changes()

    stocks = list(Stock.objects.filter(kind=Stock.KIND_EQUITY))
    funds = {f.stock_id: f for f in Fundamentals.objects.all()}
    news_by = defaultdict(list)
    for a in NewsArticle.objects.filter(published_at__gte=now - dt.timedelta(days=30)):
        news_by[a.stock_id].append(a)

    # pass 1: technicals + valuation
    rows = []
    for st in stocks:
        s = series.get(st.id)
        if s is None or s.n == 0:
            continue
        tech = metrics.technicals(s, index_series)
        _apply_reported_change(tech, s, reported.get(st.id))
        val = metrics.valuation(float(s.close[-1]), funds.get(st.id), st.is_financial, st.is_bank)
        last_date = dt.date.fromisoformat(str(s.dates[-1]))
        rows.append({"stock": st, "series": s, "tech": tech, "val": val, "sector": st.sector or "Other", "last_date": last_date})

    # Walk-forward backtests take ~85% of the run and only change when a new session is added.
    last_dates = {r["stock"].id: r["last_date"] for r in rows}
    prev_bt: dict[int, dict] = defaultdict(dict)
    for sid, hz, as_of, bt in Projection.objects.filter(as_of__in=set(last_dates.values())).values_list(
        "stock_id", "horizon", "as_of", "backtest"
    ):
        if bt and last_dates.get(sid) == as_of:
            prev_bt[sid][hz] = bt

    medians = scoring.sector_medians(rows)
    min_turnover = settings.BEEZY["MIN_AVG_TURNOVER"]

    # pass 2: projections, scores
    proj_objs, analytics, universe = [], [], []
    for i, r in enumerate(rows, 1):
        st, s, tech, val = r["stock"], r["series"], r["tech"], r["val"]
        if i % 10 == 0:
            jl.progress = f"{i}/{len(rows)} {st.ticker}"
            jl.save(update_fields=["progress"])
        med = medians.get(r["sector"], medians["__market__"])
        news = scoring.news_aggregate(news_by.get(st.id, []), now)
        last_date = r["last_date"]
        proj = projections.project(
            s.close, last_date, valuation=val, sector_pe=med.get("pe"), news_score=news["recent7"]
        )
        bt = prev_bt.get(st.id, {})
        if not bt and with_backtest and s.n > 300:
            for hz in ("1D", "1W", "1M", "1Y"):
                res = projections.backtest(s.close, hz)
                if res:
                    bt[hz] = res
        scored = scoring.score_stock(
            val, tech, proj, news, med, is_financial=st.is_financial, is_bank=st.is_bank
        )
        reasons, warnings = scoring.explain(st.ticker, val, tech, proj, news, med, scored)
        verdicts = metrics.verdicts(val, med.get("pe"), med.get("pb"))
        liquid = scoring.is_liquid(tech, min_turnover)
        if not liquid:
            warnings.append("Thinly traded — hard to buy/sell at quoted price")
        eligible = (
            liquid
            and not st.is_suspended
            and st.ticker not in catalog.NON_EQUITY
            and val.get("available")
            and not val.get("loss_making")
            and (tech.get("history_days") or 0) >= 120
        )
        stale_days = (timezone.localdate() - last_date).days
        if stale_days > 7:
            warnings.append(f"Last price is {stale_days} days old")
            eligible = False

        analytics.append(
            StockAnalytics(
                stock=st,
                as_of=last_date,
                last_price=tech.get("last"),
                change=tech.get("change"),
                change_pct=tech.get("change_pct"),
                volume=tech.get("volume"),
                metrics={**val, "verdicts": verdicts, "sector_medians": med},
                technicals=tech,
                scores={**scored, "news": news},
                composite=scored["composite"],
                risk=scored["risk"],
                expected_return_1y=proj.get("1Y", {}).get("expected_return"),
                expected_return_1m=proj.get("1M", {}).get("expected_return"),
                sentiment=news["score"],
                is_liquid=liquid,
                reasons=reasons,
                warnings=warnings,
            )
        )
        for hz, p in proj.items():
            proj_objs.append(
                Projection(
                    stock=st,
                    horizon=hz,
                    as_of=last_date,
                    target_date=dt.date.fromisoformat(p["target_date"]),
                    base_price=p["base_price"],
                    expected_price=p["expected_price"],
                    low=p["low"],
                    high=p["high"],
                    expected_return=p["expected_return"],
                    prob_up=p["prob_up"],
                    components=p["components"],
                    backtest=bt.get(hz, {}),
                )
            )
        universe.append(
            {
                "ticker": st.ticker,
                "name": st.name,
                "sector": r["sector"],
                "price": tech.get("last"),
                "change_pct": tech.get("change_pct"),
                "composite": scored["composite"],
                "risk": scored["risk"],
                "risk_adjusted": scored["risk_adjusted"],
                "pillars": scored["pillars"],
                "exp_1y": proj.get("1Y", {}).get("expected_return"),
                "exp_1m": proj.get("1M", {}).get("expected_return"),
                "prob_up_1y": proj.get("1Y", {}).get("prob_up"),
                "vol_1y": tech.get("vol_1y"),
                "market_cap": val.get("market_cap"),
                "dividend_yield": val.get("dividend_yield"),
                "pe": val.get("pe"),
                "sentiment": news["score"],
                "eligible": bool(eligible),
                "liquid": liquid,
                "reasons": reasons,
                "warnings": warnings,
            }
        )

    payload = build_recommendations(universe)
    payload["medians"] = medians
    payload["index"] = _index_summary(index_series, reported.get(index.id) if index else None)
    as_of = max((r["last_date"] for r in rows), default=None)

    # One short write transaction: readers see the old analysis or the new one, never a mix.
    with transaction.atomic():
        StockAnalytics.objects.bulk_create(
            analytics, update_conflicts=True, unique_fields=["stock"], update_fields=ANALYTICS_FIELDS
        )
        Projection.objects.bulk_create(
            proj_objs, batch_size=500, update_conflicts=True,
            unique_fields=["stock", "horizon", "as_of"], update_fields=PROJECTION_FIELDS,
        )
        return RecommendationRun.objects.create(as_of=as_of, payload=payload)


ANALYTICS_FIELDS = [
    "as_of", "last_price", "change", "change_pct", "volume", "metrics", "technicals", "scores", "composite",
    "risk", "expected_return_1y", "expected_return_1m", "sentiment", "is_liquid", "reasons", "warnings", "computed_at",
]
PROJECTION_FIELDS = [
    "target_date", "base_price", "expected_price", "low", "high", "expected_return", "prob_up",
    "components", "backtest", "created_at",
]


def build_recommendations(universe: list[dict]) -> dict:
    eligible = [u for u in universe if u["eligible"] and (u["exp_1y"] or 0) > 0]
    top5 = scoring.pick(eligible, 5)

    risks = sorted(u["risk"] for u in eligible) or [50]
    risk_median = risks[len(risks) // 2]
    high_pool = [u for u in eligible if u["risk"] >= risk_median]
    high = sorted(high_pool, key=lambda u: u["exp_1y"], reverse=True)[:3]
    low_pool = [u for u in eligible if u["composite"] >= 45]
    low = sorted(low_pool, key=lambda u: u["risk"])[:3]

    traded = [u for u in universe if u["change_pct"] is not None]
    gainers = sorted([u for u in traded if u["change_pct"] > 0], key=lambda u: -u["change_pct"])
    losers = sorted([u for u in traded if u["change_pct"] < 0], key=lambda u: u["change_pct"])
    return {
        "generated_at": timezone.now().isoformat(),
        "top5": top5,
        "high_risk_high_gain": high,
        "low_risk_low_gain": low,
        "breadth": {
            "advancers": len(gainers),
            "decliners": len(losers),
            "unchanged": len(traded) - len(gainers) - len(losers),
        },
        "universe": universe,
        "method": {
            "pillar_weights": scoring.PILLAR_WEIGHTS,
            "risk_penalty": scoring.RISK_PENALTY,
            "risk_median": risk_median,
        },
    }


def _index_summary(s, reported=None):
    if s is None or s.n == 0:
        return {}
    c = s.close
    out = {
        "value": float(c[-1]),
        "change": float(c[-1] - c[-2]) if s.n > 1 else 0.0,
        "change_pct": float(c[-1] / c[-2] - 1) * 100 if s.n > 1 else 0.0,
        "ret_1y": metrics.period_return(c, 252),
        "as_of": str(s.dates[-1]),
    }
    tech = {"last": out["value"]}
    _apply_reported_change(tech, s, reported)
    if "change" in tech:
        out["change"], out["change_pct"] = tech["change"], tech["change_pct"]
    return out


# ------------------------------------------------------------------ orchestration
REANALYSE_AFTER = dt.timedelta(hours=6)  # re-run even without new data (news decay, code updates)


def _price_fingerprint():
    since = timezone.localdate() - dt.timedelta(days=10)
    return tuple(
        PriceBar.objects.filter(date__gte=since)
        .aggregate(n=Count("id"), d=Max("date"), c=Sum("close"), v=Sum("volume"), ch=Sum("change"))
        .values()
    )


def _analysis_is_fresh() -> bool:
    last = RecommendationRun.objects.order_by("-created_at").values_list("created_at", flat=True).first()
    return last is not None and timezone.now() - last < REANALYSE_AFTER


def daily_refresh(*, news: bool = True, force_fundamentals: bool = False, full_news: bool | None = None) -> str:
    """Prices → (new listings' history) → (stale fundamentals) → news → analysis → daily pruning."""
    with ingest.job("refresh") as jl:
        jl.message = _daily_refresh(news, force_fundamentals, full_news)
        return jl.message


def _daily_refresh(news: bool, force_fundamentals: bool, full_news: bool | None) -> str:
    parts, changed = [], False
    before = _price_fingerprint()
    n = ingest.update_daily_prices()
    moved = _price_fingerprint() != before
    changed |= moved
    parts.append(f"{n} closes" + ("" if moved else " (unchanged)"))
    missing = Stock.objects.filter(
        kind=Stock.KIND_EQUITY, is_suspended=False, history_backfilled_at__isnull=True
    ).count()
    if missing and missing <= 5:  # new listings: backfill a few (60s each)
        ingest.backfill_history(only_missing=True)
        parts.append(f"backfilled {missing} new listings")
        changed = True
    stale = timezone.now() - dt.timedelta(days=7)
    if force_fundamentals or not Fundamentals.objects.filter(fetched_at__gte=stale).exists():
        parts.append(f"fundamentals {ingest.update_fundamentals()}")
        changed = True
    if not JobLog.objects.filter(job="macro", status=JobLog.OK,
                                 started_at__gte=timezone.now() - dt.timedelta(hours=20)).exists():
        try:  # CBK and EIA publish daily at most; never let a macro outage stop the price refresh
            parts.append("macro " + ("updated" if macro.update() else "unchanged"))
        except Exception as exc:
            log.warning("macro update failed: %s", exc)
            parts.append("macro failed")
    if news:
        added = ingest.update_news(full=full_news)
        parts.append(f"{added} headlines")
        changed |= added > 0
    if changed or not _analysis_is_fresh():
        run = run_analysis()
        parts.append("top5: " + ", ".join(p["ticker"] for p in run.payload.get("top5", [])))
    else:
        parts.append("analysis skipped (nothing new)")
    if not JobLog.objects.filter(job="prune", started_at__gte=timezone.now() - dt.timedelta(hours=20)).exists():
        prune()
    return "; ".join(parts)


def prune(now=None) -> str:
    """Drop rows nothing reads any more. Price history is never pruned."""
    keep = settings.BEEZY["KEEP_DAYS"]
    now = now or timezone.now()
    with ingest.job("prune") as jl:
        # analysis runs: everything from the last 2 days, then the last run of each day
        last_of_day: dict[dt.date, int] = {}
        old = RecommendationRun.objects.filter(created_at__lt=now - dt.timedelta(days=2)).order_by("created_at")
        for rid, created in old.values_list("id", "created_at"):
            last_of_day[timezone.localtime(created).date()] = rid
        cutoff = timezone.localtime(now).date() - dt.timedelta(days=keep["runs"])
        keep_ids = [rid for day, rid in last_of_day.items() if day >= cutoff]
        runs = old.exclude(id__in=keep_ids).delete()[0]
        jobs = JobLog.objects.filter(started_at__lt=now - dt.timedelta(days=keep["jobs"])).exclude(
            status=JobLog.RUNNING
        ).delete()[0]
        projs = Projection.objects.filter(as_of__lt=now.date() - dt.timedelta(days=keep["projections"])).delete()[0]
        heads = NewsArticle.objects.filter(published_at__lt=now - dt.timedelta(days=keep["news"])).delete()[0]
        with connection.cursor() as c:
            c.execute("PRAGMA optimize")
        jl.message = f"removed {runs} runs, {jobs} job logs, {projs} projections, {heads} headlines"
        return jl.message


def needs_bootstrap() -> bool:
    """An empty or mostly history-less database that hasn't been seeded in the last day."""
    missing = Stock.objects.filter(kind=Stock.KIND_EQUITY, is_suspended=False, history_backfilled_at__isnull=True).count()
    empty = not Stock.objects.exists()
    if not (empty or missing > 5):
        return False
    return not JobLog.objects.filter(job="history", started_at__gte=timezone.now() - dt.timedelta(hours=20)).exists()


def bootstrap(history: bool = True) -> str:
    """First-time load. History respects AFX's 60s crawl delay (≈75 min, resumable);
    fundamentals and news run alongside it."""
    ingest.update_daily_prices()

    def side():
        try:
            ingest.update_fundamentals()
            ingest.update_news(full=True)
            macro.update()
        except Exception:
            log.exception("bootstrap side jobs failed")
        finally:
            connection.close()

    t = threading.Thread(target=side, daemon=True, name="beezy-bootstrap-side")
    t.start()
    if history:
        ingest.backfill_history(only_missing=True)
    t.join()
    run = run_analysis()
    return "bootstrap done; top5: " + ", ".join(p["ticker"] for p in run.payload.get("top5", []))


def running_job():
    return JobLog.objects.filter(status=JobLog.RUNNING, started_at__gte=timezone.now() - dt.timedelta(hours=3)).first()


def start_background(kind: str) -> bool:
    """Run a refresh in a daemon thread (UI button / API). False if any job is already running,
    in this process or another (scheduler, cron, CLI)."""
    if not locks.acquire_jobs(0):
        return False

    def work():
        try:
            close_old_connections()
            if kind == "prices":
                ingest.update_daily_prices()
                run_analysis()
            elif kind == "news":
                ingest.update_news()
                run_analysis(with_backtest=False)
            elif kind == "analysis":
                run_analysis()
            else:
                daily_refresh()
        except Exception:  # logged in JobLog
            log.exception("background %s failed", kind)
        finally:
            close_old_connections()
            locks.release_jobs()

    try:
        threading.Thread(target=work, daemon=True, name=f"beezy-{kind}").start()
    except Exception:
        locks.release_jobs()
        raise
    return True
