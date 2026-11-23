"""Full-page views (thin shells; data arrives via the Ninja API and HTMX fragments)."""

from django.db import connection
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import queries
from .services import catalog


@require_GET
def dashboard(request):
    ctx = {
        "summary": queries.market_summary(),
        "recs": queries.recommendations(),
        "tape": [r for r in queries.stock_rows(sort="market_cap") if not r["suspended"]][:40],
        "nav": "dashboard",
    }
    return render(request, "market/dashboard.html", ctx)


@require_GET
def screener(request):
    sort = request.GET.get("sort", "risk_adjusted")
    ctx = {
        "summary": queries.market_summary(),
        "sectors": queries.sectors(),
        "rows": queries.stock_rows(sort=sort),
        "nav": "screener",
    }
    return render(request, "market/screener.html", ctx)


@require_GET
def stock(request, ticker: str):
    d = queries.stock_detail(ticker)
    if d is None:
        raise Http404("Unknown ticker")
    m = d["metrics"]
    v = m.get("verdicts", {})
    sections = [
        ("Valuation", [
            ("pe", "Price-to-Earnings (P/E)", m.get("pe"), "x", "Price ÷ EPS. What you pay for KES 1 of earnings."),
            ("peg", "PEG ratio", m.get("peg"), "x", f"P/E ÷ EPS growth ({m.get('growth_source') or 'n/a'}). Below 1.0 may signal undervaluation."),
            ("pb", "Price-to-Book (P/B)", m.get("pb"), "x", "Price ÷ book value per share. Key for banks and asset-heavy firms."),
            ("ps", "Price-to-Sales (P/S)", m.get("ps"), "x", "Market cap ÷ annual revenue."),
        ]),
        ("Profitability & cash flow", [
            ("eps", "Earnings per share (TTM)", m.get("eps"), "kes", "Net income ÷ shares outstanding. Look for steady growth."),
            ("roe", "Return on equity", m.get("roe"), "pct", "Net income ÷ shareholders' equity. 15%+ is strong."),
            ("fcf", "Free cash flow", m.get("fcf"), "big", "Operating cash flow − capital expenditure."),
            ("gross_margin", "Gross margin", m.get("gross_margin"), "pct", "Gross profit ÷ revenue: pricing power."),
        ]),
        ("Debt & dividends", [
            ("debt_to_equity", "Debt-to-equity", m.get("debt_to_equity"), "x", "Total debt ÷ equity. Above 2.0 flags leverage risk."),
            ("dividend_yield", "Dividend yield", m.get("dividend_yield"), "pct", "Annual dividend ÷ price."),
            ("payout_ratio", "Payout ratio", m.get("payout_ratio"), "pct", "Dividend ÷ EPS. Cover = EPS ÷ dividend."),
        ]),
    ]
    ctx = {
        "d": d,
        "m": m,
        "t": d["technicals"],
        "verdicts": v,
        "sections": sections,
        "summary": queries.market_summary(),
        "pillars": (d["scores"] or {}).get("pillars", {}),
        "news_agg": (d["scores"] or {}).get("news", {}),
        "non_equity": d["ticker"] in catalog.NON_EQUITY,
        "nav": "stock",
    }
    return render(request, "market/stock.html", ctx)


@require_GET
def calculator(request):
    from .services.portfolio import PROFILES

    return render(request, "market/calculator.html", {"summary": queries.market_summary(), "profiles": PROFILES, "nav": "calculator"})


@require_GET
def experiments_page(request):
    tab = request.GET.get("tab", "pca")
    tab = tab if tab in ("pca", "eigen", "markov", "accuracy", "notes") else "pca"
    return render(request, "market/experiments.html", {"summary": queries.market_summary(), "tab": tab, "nav": "experiments"})


@require_GET
def factors_page(request):
    return render(request, "market/factors.html", {
        "summary": queries.market_summary(), "x": queries.factors_overview(), "nav": "factors",
    })


@require_GET
def data_page(request):
    from .downloads import DATASETS
    from .glossary import GLOSSARY
    from .models import Stock

    short = {"stocks": "Stocks", "prices": "Prices", "fundamentals": "Fundamentals", "projections": "Projections",
             "news": "News", "picks": "Picks", "accuracy": "Accuracy", "macro": "Macro", "pca": "PCA",
             "eigenvalues": "Eigenvalues", "markov": "Markov"}
    datasets = [{"key": d.key, "short": short.get(d.key, d.title), "title": d.title, "description": d.description,
                 "filters": d.filters, "count": d.count()}
                for d in DATASETS.values() if d.key != "glossary"]
    tickers = list(Stock.objects.exclude(kind=Stock.KIND_INDEX).order_by("ticker").values_list("ticker", "name"))
    tab = request.GET.get("tab", "stocks")
    if tab not in {d["key"] for d in datasets} | {"glossary"}:
        tab = "stocks"
    from .services.macro import SERIES

    return render(request, "market/data.html", {
        "summary": queries.market_summary(), "datasets": datasets, "tickers": tickers,
        "macro_series": [(k, name) for k, (name, _, _) in SERIES.items()],
        "glossary": GLOSSARY, "term_count": sum(len(items) for _, items in GLOSSARY), "nav": "data", "tab": tab,
    })


@require_GET
def news_page(request):
    return render(request, "market/news.html", {"summary": queries.market_summary(), "nav": "news"})


@require_GET
def method(request):
    from .services import projections, scoring

    ctx = {
        "summary": queries.market_summary(),
        "pillars": scoring.PILLAR_WEIGHTS,
        "drift": projections.DRIFT_WEIGHTS,
        "anchors": projections.ANCHOR_WEIGHTS,
        "nav": "method",
    }
    return render(request, "market/method.html", ctx)


@never_cache
@require_GET
def healthz(request):
    """Liveness for the hosting platform: the app is up and the database answers."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
    except Exception:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
