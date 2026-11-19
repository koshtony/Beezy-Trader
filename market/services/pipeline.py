"""Analysis pipeline: rebuild analytics, projections and recommendations from stored data."""

import datetime as dt
import logging
import threading
from collections import defaultdict

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, transaction
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
from . import catalog, ingest, metrics, projections, scoring

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
        rows.append({"stock": st, "series": s, "tech": tech, "val": val, "sector": st.sector or "Other"})

    medians = scoring.sector_medians(rows)
    min_turnover = settings.BEEZY["MIN_AVG_TURNOVER"]

    # pass 2: projections, scores
    proj_objs, universe = [], []
    for i, r in enumerate(rows, 1):
        st, s, tech, val = r["stock"], r["series"], r["tech"], r["val"]
        if i % 10 == 0:
            jl.progress = f"{i}/{len(rows)} {st.ticker}"
            jl.save(update_fields=["progress"])
        med = medians.get(r["sector"], medians["__market__"])
        news = scoring.news_aggregate(news_by.get(st.id, []), now)
        last_date = dt.date.fromisoformat(str(s.dates[-1]))
        proj = projections.project(
            s.close, last_date, valuation=val, sector_pe=med.get("pe"), news_score=news["recent7"]
        )
        bt = {}
        if with_backtest and s.n > 300:
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

        StockAnalytics.objects.update_or_create(
            stock=st,
            defaults={
                "as_of": last_date,
                "last_price": tech.get("last"),
                "change": tech.get("change"),
                "change_pct": tech.get("change_pct"),
                "volume": tech.get("volume"),
                "metrics": {**val, "verdicts": verdicts, "sector_medians": med},
                "technicals": tech,
                "scores": {**scored, "news": news},
                "composite": scored["composite"],
                "risk": scored["risk"],
                "expected_return_1y": proj.get("1Y", {}).get("expected_return"),
                "expected_return_1m": proj.get("1M", {}).get("expected_return"),
                "sentiment": news["score"],
                "is_liquid": liquid,
                "reasons": reasons,
                "warnings": warnings,
            },
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

    with transaction.atomic():
        for p in proj_objs:
            Projection.objects.update_or_create(
                stock=p.stock,
                horizon=p.horizon,
                as_of=p.as_of,
                defaults={
                    f: getattr(p, f)
                    for f in (
                        "target_date", "base_price", "expected_price", "low", "high",
                        "expected_return", "prob_up", "components", "backtest",
                    )
                },
            )

    payload = build_recommendations(universe)
    payload["medians"] = medians
    payload["index"] = _index_summary(index_series, reported.get(index.id) if index else None)
    as_of = max((dt.date.fromisoformat(str(r["series"].dates[-1])) for r in rows), default=None)
    return RecommendationRun.objects.create(as_of=as_of, payload=payload)


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
def daily_refresh(*, news: bool = True, force_fundamentals: bool = False) -> str:
    parts = []
    n = ingest.update_daily_prices()
    parts.append(f"{n} closes")
    missing = Stock.objects.filter(
        kind=Stock.KIND_EQUITY, is_suspended=False, history_backfilled_at__isnull=True
    ).count()
    if missing and missing <= 5:  # new listings: backfill a few (60s each)
        ingest.backfill_history(only_missing=True)
        parts.append(f"backfilled {missing} new listings")
    stale = timezone.now() - dt.timedelta(days=7)
    if force_fundamentals or not Fundamentals.objects.filter(fetched_at__gte=stale).exists():
        parts.append(f"fundamentals {ingest.update_fundamentals()}")
    if news:
        parts.append(f"{ingest.update_news()} headlines")
    run = run_analysis()
    parts.append("top5: " + ", ".join(p["ticker"] for p in run.payload.get("top5", [])))
    return "; ".join(parts)


_bg_lock = threading.Lock()


def running_job():
    return JobLog.objects.filter(status=JobLog.RUNNING, started_at__gte=timezone.now() - dt.timedelta(hours=3)).first()


def start_background(kind: str) -> bool:
    """Run a refresh in a daemon thread (for the UI button). Returns False if one is running."""
    if running_job() is not None:  # e.g. a CLI backfill in another process
        return False
    if not _bg_lock.acquire(blocking=False):
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
            _bg_lock.release()

    threading.Thread(target=work, daemon=True, name=f"beezy-{kind}").start()
    return True
