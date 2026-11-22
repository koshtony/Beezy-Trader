"""HTML fragment endpoints (served through Django Ninja) consumed by HTMX."""

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string
from ninja import Router

from . import queries
from .security import CALC_THROTTLE, JOB_AUTH, can_trigger_jobs
from .services import pipeline
from .services.experiments import ExperimentError
from .services.portfolio import PROFILES, CalculatorError

router = Router(tags=["ui-fragments"])

MOVER_TITLES = {
    "gainers": "Top gainers",
    "losers": "Top losers",
    "active": "Most active (turnover)",
    "volatile": "Most volatile (1Y σ)",
    "ytd": "Year-to-date leaders",
}


def _html(template: str, ctx: dict, request: HttpRequest) -> HttpResponse:
    return HttpResponse(render_to_string(f"market/partials/{template}", ctx, request=request))


@router.get("/movers", response=None, include_in_schema=False)
def movers(request: HttpRequest, kind: str = "gainers"):
    kind = kind if kind in MOVER_TITLES else "gainers"
    return _html("movers.html", {"rows": queries.movers(kind, 10), "kind": kind, "title": MOVER_TITLES[kind]}, request)


@router.get("/news", response=None, include_in_schema=False)
def news(request: HttpRequest, label: str = "", ticker: str = "", limit: int = 25):
    items = queries.news(ticker or None, label, min(limit, 100))
    return _html("news.html", {"items": items, "label": label, "ticker": ticker}, request)


@router.get("/screener", response=None, include_in_schema=False)
def screener(request: HttpRequest, sector: str = "", q: str = "", sort: str = "risk_adjusted", order: str = "desc"):
    rows = queries.stock_rows(sector=sector, q=q.strip(), sort=sort, desc=order != "asc")
    return _html("screener_rows.html", {"rows": rows}, request)


@router.get("/calculator", response=None, throttle=CALC_THROTTLE, include_in_schema=False)
def calculator(request: HttpRequest, amount: str = "100000", profile: str = "steady", horizon: str = "1", unit: str = "years", costs: str = ""):
    ctx = {"profiles": PROFILES}
    try:
        amt = float(str(amount).replace(",", "").strip() or 0)
        hz = float(horizon or 0)
        if profile not in PROFILES:
            profile = "steady"
        ctx["r"] = queries.calculator(amt, profile, hz, unit, costs in ("1", "on", "true"))
    except (ValueError, CalculatorError) as exc:
        ctx["error"] = str(exc) if isinstance(exc, CalculatorError) else "Enter a valid amount and period."
    return _html("calculator_result.html", ctx, request)


@router.get("/experiments/{kind}", response=None, include_in_schema=False)
def experiment(request: HttpRequest, kind: str, ticker: str = "NASI"):
    if kind not in ("pca", "eigen", "markov", "accuracy", "notes"):
        kind = "pca"
    try:
        data = queries.experiment(kind, ticker.upper()[:16] if kind == "markov" else "")
        return _html(f"exp_{kind}.html", {"x": data, "kind": kind}, request)
    except ExperimentError as exc:
        return _html("exp_error.html", {"error": str(exc)}, request)


@router.get("/data-preview/{dataset}", response=None, include_in_schema=False)
def data_preview(request: HttpRequest, dataset: str, ticker: str = "", start: str = "", end: str = "", label: str = ""):
    import datetime as dt
    from itertools import islice

    from .downloads import DATASETS, Params, human
    from .services.experiments import ExperimentError as ExpErr

    ds = DATASETS.get(dataset)
    if ds is None:
        return _html("exp_error.html", {"error": "Unknown dataset."}, request)
    try:
        params = Params(
            ticker=ticker.strip().upper()[:16],
            start=dt.date.fromisoformat(start) if start else None,
            end=dt.date.fromisoformat(end) if end else None,
            label=label,
        )
        rows = list(islice(ds.rows(params), 8))
    except (ValueError, ExpErr) as exc:
        return _html("exp_error.html", {"error": str(exc) or "Invalid filter."}, request)
    cols = list(rows[0].keys()) if rows else []
    table = [[human(r.get(c)) for c in cols] for r in rows]
    return _html("data_preview.html", {"cols": cols, "rows": table}, request)


@router.get("/status", response=None, include_in_schema=False)
def status(request: HttpRequest, was: str = ""):
    st = queries.job_status()
    resp = _html("status.html", {"status": st, "summary": queries.market_summary(), "can_refresh": can_trigger_jobs(request)}, request)
    if was == "running" and not st["running"]:
        resp["HX-Refresh"] = "true"  # a job just finished: reload the page with fresh data
    return resp


@router.post("/refresh", response=None, auth=JOB_AUTH, include_in_schema=False)
def refresh(request: HttpRequest, kind: str = "prices"):
    if kind not in ("prices", "news", "analysis", "daily"):
        kind = "prices"
    started = pipeline.start_background(kind)
    resp = _html("status.html", {"status": queries.job_status(), "summary": queries.market_summary(), "just_started": started, "can_refresh": True}, request)
    return resp
