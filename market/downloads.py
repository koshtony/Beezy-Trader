"""Downloadable datasets (CSV for Excel, or JSON)."""

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Callable, Iterable

from django.db.models import Max
from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone

from . import queries
from .glossary import flat as glossary_rows
from .models import Fundamentals, NewsArticle, PriceBar, Projection, Stock, StockAnalytics

# Cells starting with these characters can run as formulas in Excel/Sheets (CSV injection).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Params:
    ticker: str = ""
    start: dt.date | None = None
    end: dt.date | None = None
    label: str = ""


@dataclass
class Dataset:
    key: str
    title: str
    description: str
    rows: Callable[[Params], Iterable[dict]]
    filters: list[str] = field(default_factory=list)
    count: Callable[[], int | None] = lambda: None
    columns: list[str] = field(default_factory=list)  # header for a download with no rows yet


def _stocks(p: Params):
    for r in queries.stock_rows(sort="ticker", desc=False):
        row = {k: v for k, v in r.items() if k != "pillars"}
        for k, v in (r.get("pillars") or {}).items():
            row[f"score_{k}"] = v
        yield row


def _prices(p: Params):
    qs = PriceBar.objects.select_related(None).order_by("stock__ticker", "date")
    if p.ticker:
        qs = qs.filter(stock__ticker=p.ticker)
    if p.start:
        qs = qs.filter(date__gte=p.start)
    if p.end:
        qs = qs.filter(date__lte=p.end)
    for t, d, c, v, ch in qs.values_list("stock__ticker", "date", "close", "volume", "change").iterator(chunk_size=5000):
        yield {"ticker": t, "date": d.isoformat(), "close": c, "volume": v, "change": ch}


FUND_FIELDS = [
    "eps_ttm", "revenue_ttm", "gross_profit_ttm", "net_income_ttm", "equity", "book_value_per_share",
    "total_debt", "cash", "operating_cash_flow", "capex", "free_cash_flow", "dividend_per_share",
    "shares_outstanding", "src_pe", "src_forward_pe", "src_peg", "src_pb", "src_ps", "src_roe", "src_roa",
    "src_debt_to_equity", "src_gross_margin", "src_profit_margin", "src_payout_ratio", "src_current_ratio",
    "eps_growth_forecast", "revenue_growth_forecast", "analyst_target", "analyst_consensus", "analyst_count",
    "fiscal_period",
]


def _fundamentals(p: Params):
    qs = Fundamentals.objects.select_related("stock").order_by("stock__ticker")
    if p.ticker:
        qs = qs.filter(stock__ticker=p.ticker)
    for f in qs:
        row = {"ticker": f.stock.ticker, "name": f.stock.name, "sector": f.stock.sector}
        row.update({k: getattr(f, k) for k in FUND_FIELDS})
        row["eps_history"] = "; ".join(f"{h['period']}: {h['eps']}" for h in (f.eps_history or []))
        row["source"], row["fetched_at"] = f.source, f.fetched_at.isoformat()
        yield row


def _projections(p: Params):
    latest = dict(Projection.objects.values_list("stock_id").annotate(m=Max("as_of")))
    qs = Projection.objects.select_related("stock").order_by("stock__ticker", "target_date")
    if p.ticker:
        qs = qs.filter(stock__ticker=p.ticker)
    for pr in qs:
        if pr.as_of != latest.get(pr.stock_id):
            continue
        bt = pr.backtest or {}
        yield {
            "ticker": pr.stock.ticker, "horizon": pr.horizon, "as_of": pr.as_of.isoformat(),
            "target_date": pr.target_date.isoformat(), "base_price": pr.base_price,
            "expected_price": pr.expected_price, "low_90": pr.low, "high_90": pr.high,
            "expected_return": pr.expected_return, "prob_up": pr.prob_up,
            "backtest_hit_rate": bt.get("hit_rate"), "backtest_band_coverage": bt.get("band_coverage"),
        }


def _accuracy(p: Params):
    from .services import accuracy

    for r in accuracy.live()["records"]:
        if p.ticker and r["ticker"] != p.ticker:
            continue
        yield {
            "ticker": r["ticker"], "horizon": r["horizon"], "made_on": r["made"].isoformat(),
            "checked_on": r["target"].isoformat(), "price_then": r["base"], "forecast_price": r["forecast"],
            "low_90": r["low"], "high_90": r["high"], "actual_price": r["actual"],
            "forecast_change": r["forecast_change"], "actual_change": r["actual_change"],
            "abs_error": r["error"], "no_change_error": r["naive_error"], "direction": r["direction"],
            "inside_range": r["in_range"], "prob_up": r["prob_up"],
        }


ACCURACY_COLUMNS = [
    "ticker", "horizon", "made_on", "checked_on", "price_then", "forecast_price", "low_90", "high_90", "actual_price",
    "forecast_change", "actual_change", "abs_error", "no_change_error", "direction", "inside_range", "prob_up",
]


def _news(p: Params):
    qs = NewsArticle.objects.select_related("stock").order_by("-published_at")
    if p.ticker:
        qs = qs.filter(stock__ticker=p.ticker)
    if p.label in ("positive", "negative", "neutral"):
        qs = qs.filter(label=p.label)
    if p.start:
        qs = qs.filter(published_at__date__gte=p.start)
    if p.end:
        qs = qs.filter(published_at__date__lte=p.end)
    for a in qs.iterator(chunk_size=2000):
        yield {
            "ticker": a.stock.ticker, "published_at": a.published_at.isoformat(), "title": a.title,
            "source": a.source, "label": a.label, "sentiment": a.sentiment, "url": a.url,
        }


def _picks(p: Params):
    rec = queries.recommendations()
    for group, key in (("Top 5", "top5"), ("High risk / high gain", "high_risk_high_gain"), ("Low risk / steady", "low_risk_low_gain")):
        for i, r in enumerate(rec.get(key, []), 1):
            yield {
                "list": group, "rank": i, "ticker": r["ticker"], "name": r["name"], "sector": r["sector"],
                "price": r.get("price"), "exp_1y": r.get("exp_1y"), "prob_up_1y": r.get("prob_up_1y"),
                "score": r.get("composite"), "risk": r.get("risk"), "risk_adjusted": r.get("risk_adjusted"),
                "reasons": "; ".join(r.get("reasons", [])), "warnings": "; ".join(r.get("warnings", [])),
                "as_of": rec.get("as_of"),
            }


def _pca(p: Params):
    x = queries.experiment("pca")
    by = {}
    for c in x["components"]:
        for item in c["loadings"]:
            by.setdefault(item["ticker"], dict(ticker=item["ticker"], name=item["name"], sector=item["sector"]))[f"pc{c['pc']}_loading"] = item["loading"]
    yield from sorted(by.values(), key=lambda r: -r.get("pc1_loading", 0))


def _eigen(p: Params):
    x = queries.experiment("eigen")
    vals = x["eigenvalues"]
    total = sum(vals) or 1
    cum = 0.0
    for i, v in enumerate(vals, 1):
        cum += v / total
        yield {"rank": i, "eigenvalue": v, "explained": v / total, "cumulative": cum,
               "above_noise_bound": v > x["lambda_plus"], "noise_bound_lambda_plus": x["lambda_plus"]}


def _markov(p: Params):
    x = queries.experiment("markov", p.ticker or "NASI")
    for row in x["matrix"]:
        for j, cell in enumerate(row["cells"]):
            yield {"ticker": x["ticker"], "from_state": row["state"], "to_state": x["states"][j],
                   "probability": cell["p"], "transitions": cell["n"]}
    for state, pi in zip(x["states"], x["stationary"]):
        yield {"ticker": x["ticker"], "from_state": "long run (π)", "to_state": state, "probability": pi, "transitions": None}


def _glossary(p: Params):
    yield from glossary_rows()


DATASETS: dict[str, Dataset] = {d.key: d for d in [
    Dataset("stocks", "Stocks & key metrics", "Every counter with price, valuation ratios, returns, volatility, projections and scores.",
            _stocks, count=lambda: StockAnalytics.objects.count()),
    Dataset("prices", "Daily prices", "End-of-day closes (and volume where recorded) for every counter and NASI.",
            _prices, ["ticker", "start", "end"], count=lambda: PriceBar.objects.count()),
    Dataset("fundamentals", "Fundamentals", "Raw inputs: revenue, profit, equity, debt, cash flow, dividends, EPS history and analyst data.",
            _fundamentals, ["ticker"], count=lambda: Fundamentals.objects.count()),
    Dataset("projections", "Projections", "Latest 1D / 1W / 1M / 1Y projections with 90% ranges, probability of gain and backtest results.",
            _projections, ["ticker"], count=lambda: StockAnalytics.objects.count() * 4),
    Dataset("accuracy", "Forecast accuracy", "Every published forecast that has reached its target date, with the actual price, "
            "error, direction and whether it landed inside the 90% range.", _accuracy, ["ticker"],
            columns=ACCURACY_COLUMNS),
    Dataset("news", "News & sentiment", "Headlines with source, date, link and positive / negative / neutral label.",
            _news, ["ticker", "label", "start", "end"], count=lambda: NewsArticle.objects.count()),
    Dataset("picks", "Picks", "Top 5, high risk / high gain and low risk / steady lists with reasons and warnings.", _picks),
    Dataset("pca", "PCA loadings", "Each counter's loading on the first three principal components (Labs).", _pca),
    Dataset("eigenvalues", "Eigenvalues", "Eigenvalue spectrum with variance explained and the noise bound (Labs).", _eigen),
    Dataset("markov", "Markov transition matrix", "Day-to-day state transition odds and long-run distribution for one series (Labs).",
            _markov, ["ticker"]),
    Dataset("glossary", "Glossary", "Every term used on the site with its definition.", _glossary, count=lambda: len(glossary_rows())),
]}


def _cell(v):
    if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
        return "'" + v
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return "" if v is None else v


def human(v) -> tuple[str, bool]:
    """Display form for previews: thousands separators and sensible decimals. Returns (text, is_number)."""
    if isinstance(v, bool) or v is None:
        return _cell(v), False
    if isinstance(v, int):
        return f"{v:,}", True
    if isinstance(v, float):
        if v != v:  # NaN
            return "", False
        a = abs(v)
        if a >= 1000:
            return (f"{v:,.0f}" if v.is_integer() else f"{v:,.2f}"), True
        if a >= 1:
            return f"{v:,.2f}", True
        if a == 0:
            return "0", True
        return f"{v:.4f}".rstrip("0").rstrip("."), True
    return str(_cell(v)), False


class _Echo:
    def write(self, value):
        return value


def respond(ds: Dataset, params: Params, fmt: str):
    rows = ds.rows(params)
    stamp = timezone.localdate().isoformat()
    name = f"beezy-markets_{ds.key}{'_' + params.ticker if params.ticker else ''}_{stamp}"
    if fmt == "json":
        data = list(rows)
        resp = HttpResponse(json.dumps(data, default=str, indent=1), content_type="application/json")
        resp["Content-Disposition"] = f'attachment; filename="{name}.json"'
        return resp

    def stream():
        writer = None
        out = _Echo()
        yield "﻿"  # BOM so Excel reads UTF-8 (KES, π, χ²) correctly
        for row in rows:
            if writer is None:
                writer = csv.DictWriter(out, fieldnames=list(row.keys()), extrasaction="ignore")
                yield writer.writeheader()
            yield writer.writerow({k: _cell(v) for k, v in row.items()})
        if writer is None and ds.columns:
            yield csv.DictWriter(out, fieldnames=ds.columns).writeheader()

    resp = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return resp
