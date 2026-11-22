"""Read-side helpers shared by the JSON API and the HTMX fragment endpoints."""

import datetime as dt
import functools
import hashlib

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from .models import JobLog, NewsArticle, PriceBar, Projection, RecommendationRun, Stock, StockAnalytics
from .security import safe_url
from .services import accuracy, catalog, experiments, pipeline, portfolio

HORIZON_ORDER = {"1D": 0, "1W": 1, "1M": 2, "1Y": 3}


def data_version() -> int:
    """Id of the latest analysis run, re-read at most every 10 s. Part of every cache key, so a run
    finished by another process (scheduler, cron, another web worker) shows up within seconds."""
    v = cache.get("bz:ver")
    if v is None:
        v = RecommendationRun.objects.order_by("-id").values_list("id", flat=True).first() or 0
        cache.set("bz:ver", v, 10)
    return v


def cached(prefix: str, ttl: int = 300):
    """Cache a function's return value keyed by its arguments and the data version."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            raw = repr((args, sorted(kwargs.items())))
            key = f"bz:{prefix}:{data_version()}:{hashlib.md5(raw.encode()).hexdigest()}"
            hit = cache.get(key)
            if hit is not None:
                return hit
            val = fn(*args, **kwargs)
            cache.set(key, val, ttl)
            return val

        return wrapper

    return deco


def latest_run() -> RecommendationRun | None:
    return RecommendationRun.objects.order_by("-created_at").first()


@cached("summary", 120)
def market_summary() -> dict:
    run = latest_run()
    payload = run.payload if run else {}
    last_prices = JobLog.objects.filter(job="prices", status=JobLog.OK).first()
    return {
        "as_of": run.as_of.isoformat() if run and run.as_of else None,
        "analysed_at": run.created_at.isoformat() if run else None,
        "prices_updated_at": last_prices.finished_at.isoformat() if last_prices and last_prices.finished_at else None,
        "index": payload.get("index", {}),
        "breadth": payload.get("breadth", {}),
        "stocks": Stock.objects.filter(kind=Stock.KIND_EQUITY).count(),
        "with_history": Stock.objects.filter(history_backfilled_at__isnull=False).count(),
        "headlines": NewsArticle.objects.count(),
    }


def _row(a: StockAnalytics) -> dict:
    st = a.stock
    m, t, s = a.metrics or {}, a.technicals or {}, a.scores or {}
    return {
        "ticker": st.ticker,
        "name": st.name,
        "sector": st.sector,
        "price": a.last_price,
        "change": a.change,
        "change_pct": a.change_pct,
        "volume": a.volume,
        "market_cap": m.get("market_cap"),
        "pe": m.get("pe"),
        "peg": m.get("peg"),
        "pb": m.get("pb"),
        "ps": m.get("ps"),
        "eps": m.get("eps"),
        "roe": m.get("roe"),
        "gross_margin": m.get("gross_margin"),
        "fcf_yield": m.get("fcf_yield"),
        "debt_to_equity": m.get("debt_to_equity"),
        "dividend_yield": m.get("dividend_yield"),
        "payout_ratio": m.get("payout_ratio"),
        "vol_1y": t.get("vol_1y"),
        "ret_1m": t.get("ret_1m"),
        "ret_ytd": t.get("ret_ytd"),
        "rsi_14": t.get("rsi_14"),
        "avg_turnover": t.get("avg_turnover"),
        "exp_1m": a.expected_return_1m,
        "exp_1y": a.expected_return_1y,
        "composite": a.composite,
        "risk": a.risk,
        "risk_adjusted": s.get("risk_adjusted"),
        "sentiment": a.sentiment,
        "liquid": a.is_liquid,
        "suspended": st.is_suspended,
        "pillars": s.get("pillars", {}),
    }


@cached("rows", 300)
def stock_rows(sector: str = "", q: str = "", sort: str = "composite", desc: bool = True) -> list[dict]:
    qs = StockAnalytics.objects.select_related("stock").filter(stock__kind=Stock.KIND_EQUITY)
    if sector:
        qs = qs.filter(stock__sector=sector)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(stock__ticker__icontains=q) | Q(stock__name__icontains=q))
    rows = [_row(a) for a in qs]

    def key(r):
        v = r.get(sort)
        return (v is None, -(v or 0) if desc else (v or 0))

    rows.sort(key=key)
    return rows


def sectors() -> list[str]:
    return sorted(
        s
        for s in Stock.objects.filter(kind=Stock.KIND_EQUITY).values_list("sector", flat=True).distinct()
        if s
    )


@cached("detail", 300)
def stock_detail(ticker: str) -> dict | None:
    st = Stock.objects.filter(ticker=ticker.upper()).first()
    if st is None:
        return None
    a = StockAnalytics.objects.filter(stock=st).first()
    projs = latest_projections(st)
    f = getattr(st, "fundamentals", None)
    return {
        "ticker": st.ticker,
        "name": st.name,
        "sector": st.sector,
        "suspended": st.is_suspended,
        "kind": st.kind,
        "row": _row(a) if a else None,
        "metrics": (a.metrics if a else {}) or {},
        "technicals": (a.technicals if a else {}) or {},
        "scores": (a.scores if a else {}) or {},
        "reasons": a.reasons if a else [],
        "warnings": a.warnings if a else [],
        "as_of": a.as_of.isoformat() if a and a.as_of else None,
        "projections": projs,
        "fundamentals_source": f.source if f else None,
        "fundamentals_fetched": f.fetched_at.isoformat() if f else None,
    }


def latest_projections(st: Stock) -> list[dict]:
    last = Projection.objects.filter(stock=st).aggregate(m=Max("as_of"))["m"]
    if not last:
        return []
    out = []
    for p in Projection.objects.filter(stock=st, as_of=last):
        out.append(
            {
                "horizon": p.horizon,
                "label": dict(Projection.HORIZONS)[p.horizon],
                "as_of": p.as_of.isoformat(),
                "target_date": p.target_date.isoformat(),
                "base_price": p.base_price,
                "expected_price": p.expected_price,
                "low": p.low,
                "high": p.high,
                "expected_return": p.expected_return,
                "prob_up": p.prob_up,
                "components": p.components,
                "backtest": p.backtest,
            }
        )
    out.sort(key=lambda p: HORIZON_ORDER[p["horizon"]])
    return out


@cached("history", 300)
def price_history(ticker: str, days: int = 0) -> list[dict]:
    qs = PriceBar.objects.filter(stock__ticker=ticker.upper()).order_by("date")
    if days:
        qs = qs.filter(date__gte=timezone.localdate() - dt.timedelta(days=days))
    return [
        {"date": d.isoformat(), "close": c, "volume": v}
        for d, c, v in qs.values_list("date", "close", "volume")
    ]


@cached("chart", 300)
def chart_payload(ticker: str) -> dict | None:
    detail = stock_detail(ticker)
    if detail is None:
        return None
    hist = price_history(ticker)
    closes = [h["close"] for h in hist]

    def rolling(n):
        out, s = [], 0.0
        for i, c in enumerate(closes):
            s += c
            if i >= n:
                s -= closes[i - n]
            out.append(round(s / n, 4) if i >= n - 1 else None)
        return out

    projs = detail["projections"]
    fan = {"dates": [], "mid": [], "low": [], "high": [], "labels": []}
    if hist and projs:
        fan["dates"].append(hist[-1]["date"])
        for k in ("mid", "low", "high"):
            fan[k].append(hist[-1]["close"])
        fan["labels"].append("Last close")
        for p in projs:
            fan["dates"].append(p["target_date"])
            fan["mid"].append(round(p["expected_price"], 4))
            fan["low"].append(round(p["low"], 4))
            fan["high"].append(round(p["high"], 4))
            fan["labels"].append(p["label"])
    return {
        "ticker": detail["ticker"],
        "dates": [h["date"] for h in hist],
        "close": closes,
        "volume": [h["volume"] for h in hist],
        "sma50": rolling(50),
        "sma200": rolling(200),
        "projection": fan,
        "eps_history": detail["metrics"].get("eps_history", []),
        "pillars": detail["scores"].get("pillars", {}),
    }


def news(ticker: str | None = None, label: str = "", limit: int = 30) -> list[dict]:
    qs = NewsArticle.objects.select_related("stock")
    if ticker:
        qs = qs.filter(stock__ticker=ticker.upper())
    if label in ("positive", "negative", "neutral"):
        qs = qs.filter(label=label)
    out = []
    seen = set()
    for a in qs.order_by("-published_at")[: limit * 2]:
        if a.title in seen:  # same story matched to several counters
            continue
        seen.add(a.title)
        out.append(
            {
                "ticker": a.stock.ticker,
                "title": a.title,
                "url": safe_url(a.url),
                "source": a.source,
                "published_at": a.published_at.isoformat(),
                "published": a.published_at,
                "sentiment": a.sentiment,
                "label": a.label,
                "terms": a.matched_terms,
            }
        )
        if len(out) >= limit:
            break
    return out


@cached("movers", 300)
def movers(kind: str = "gainers", limit: int = 10) -> list[dict]:
    rows = [
        dict(r, turnover=(r["volume"] or 0) * r["price"])
        for r in stock_rows()
        if not r["suspended"] and r["ticker"] not in catalog.NON_EQUITY and r["price"] is not None
    ]
    if kind == "gainers":
        rows = sorted([r for r in rows if (r["change_pct"] or 0) > 0], key=lambda r: -r["change_pct"])
    elif kind == "losers":
        rows = sorted([r for r in rows if (r["change_pct"] or 0) < 0], key=lambda r: r["change_pct"])
    elif kind == "active":
        rows = sorted([r for r in rows if r["turnover"]], key=lambda r: -r["turnover"])
    elif kind == "volatile":
        rows = sorted([r for r in rows if r["vol_1y"]], key=lambda r: -r["vol_1y"])
    elif kind == "ytd":
        rows = sorted([r for r in rows if r["ret_ytd"] is not None], key=lambda r: -r["ret_ytd"])
    return rows[:limit]


def recommendations() -> dict:
    run = latest_run()
    if run is None:
        return {}
    p = run.payload
    return {
        "as_of": run.as_of.isoformat() if run.as_of else None,
        "generated_at": p.get("generated_at"),
        "top5": p.get("top5", []),
        "high_risk_high_gain": p.get("high_risk_high_gain", []),
        "low_risk_low_gain": p.get("low_risk_low_gain", []),
        "method": p.get("method", {}),
    }


@cached("scatter", 300)
def scatter() -> list[dict]:
    run = latest_run()
    if run is None:
        return []
    top = {u["ticker"] for u in run.payload.get("top5", [])}
    hi = {u["ticker"] for u in run.payload.get("high_risk_high_gain", [])}
    lo = {u["ticker"] for u in run.payload.get("low_risk_low_gain", [])}
    out = []
    for u in run.payload.get("universe", []):
        if u["exp_1y"] is None or u["ticker"] in catalog.NON_EQUITY:
            continue
        group = "Top 5" if u["ticker"] in top else "High risk / high gain" if u["ticker"] in hi else "Low risk / steady" if u["ticker"] in lo else ("Eligible" if u["eligible"] else "Not eligible")
        out.append(
            {
                "ticker": u["ticker"],
                "name": u["name"],
                "risk": u["risk"],
                "exp_1y": u["exp_1y"],
                "composite": u["composite"],
                "market_cap": u["market_cap"],
                "group": group,
            }
        )
    return out


def calculator(amount: float, profile: str, horizon: float, unit: str, costs: bool = True) -> dict:
    """Portfolio simulation + side-by-side comparison, cached per inputs and analysis run."""
    run = latest_run()
    key = f"bz:calc:{run.pk if run else 0}:{amount:.2f}:{profile}:{horizon:g}:{unit}:{int(costs)}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    result = portfolio.simulate(amount, profile, horizon, unit, include_costs=costs, run=run)
    result["compare"] = portfolio.compare(amount, horizon, unit, include_costs=costs)
    cache.set(key, result, 600)
    return result


def experiment(kind: str, ticker: str = "") -> dict:
    """Cached experiment results, refreshed whenever a new analysis run lands."""
    run = latest_run()
    key = f"bz:exp:{run.pk if run else 0}:{kind}:{ticker}"
    hit = cache.get(key)
    if hit is None and kind == "notes":
        parts, errors = {}, {}
        for k in ("pca", "eigen", "markov"):
            try:
                parts[k] = experiment(k)
            except experiments.ExperimentError as exc:
                errors[k] = str(exc)
        from .services import notes

        hit = notes.build(parts.get("pca"), parts.get("eigen"), parts.get("markov"), errors,
                          acc=experiment("accuracy")["notes"])
        hit["as_of"] = run.as_of.isoformat() if run and run.as_of else None
        cache.set(key, hit, 3600)
    if hit is None:
        if kind == "pca":
            hit = experiments.pca()
        elif kind == "eigen":
            hit = experiments.eigen()
        elif kind == "accuracy":
            hit = accuracy.report()
        else:
            choices = experiments.markov_tickers()
            if (ticker or "NASI") == "NASI" and choices and choices[0]["ticker"] != "NASI":
                ticker = choices[0]["ticker"]  # no index history yet: default to the first counter
            hit = experiments.markov(ticker or "NASI")
            hit["choices"] = choices
        cache.set(key, hit, 3600)
    return hit


def job_status() -> dict:
    running = pipeline.running_job()
    recent = list(JobLog.objects.all()[:8])
    return {
        "running": bool(running),
        "current": {"job": running.job, "progress": running.progress, "started_at": running.started_at.isoformat()} if running else None,
        "recent": [
            {
                "job": j.job,
                "status": j.status,
                "message": j.message,
                "progress": j.progress,
                "started_at": j.started_at.isoformat(),
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in recent
        ],
    }
