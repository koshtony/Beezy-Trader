"""Django Ninja API: JSON endpoints (/api/...) plus HTML fragments for HTMX (/api/ui/...)."""

from typing import Literal

from django.conf import settings

from django.http import HttpRequest
from ninja import NinjaAPI, Query, Router
from ninja.errors import HttpError

from . import queries
from .schemas import (
    JobStarted,
    NewsSchema,
    PricePoint,
    ProjectionSchema,
    RecommendationSchema,
    ScatterPoint,
    StockDetail,
    StockRow,
    SummarySchema,
)
from .security import API_THROTTLE, CALC_THROTTLE, DOWNLOAD_THROTTLE, JOB_AUTH
from .services import pipeline
from .services.experiments import ExperimentError
from .services.portfolio import CalculatorError
from .ui import router as ui_router

api = NinjaAPI(
    title="Beezy Markets API",
    version="1.0.0",
    description=(
        "Nairobi Securities Exchange trading assistant: daily prices, fundamentals, valuation "
        "metrics, projections, news sentiment and ranked picks. Not investment advice."
    ),
    urls_namespace="api",
    throttle=API_THROTTLE,
    docs_url="/docs" if (settings.DEBUG or settings.BEEZY["API_DOCS"]) else None,
)

router = Router(tags=["market"])


@router.get("/market/summary", response=SummarySchema, summary="Index level, breadth and data freshness")
def market_summary(request: HttpRequest):
    return queries.market_summary()


@router.get("/stocks", response=list[StockRow], summary="All stocks with key metrics and scores")
def list_stocks(
    request: HttpRequest,
    sector: str = "",
    q: str = "",
    sort: str = "composite",
    order: Literal["asc", "desc"] = "desc",
):
    return queries.stock_rows(sector=sector, q=q, sort=sort, desc=order == "desc")


@router.get("/sectors", response=list[str])
def list_sectors(request: HttpRequest):
    return queries.sectors()


@router.get("/stocks/{ticker}", response=StockDetail, summary="Metrics, verdicts, scores and projections")
def stock_detail(request: HttpRequest, ticker: str):
    d = queries.stock_detail(ticker)
    if d is None:
        raise HttpError(404, f"Unknown ticker {ticker}")
    return d


@router.get("/stocks/{ticker}/history", response=list[PricePoint])
def stock_history(request: HttpRequest, ticker: str, days: int = Query(0, ge=0, description="0 = full history")):
    return queries.price_history(ticker, days)


@router.get("/stocks/{ticker}/projections", response=list[ProjectionSchema])
def stock_projections(request: HttpRequest, ticker: str):
    d = queries.stock_detail(ticker)
    if d is None:
        raise HttpError(404, f"Unknown ticker {ticker}")
    return d["projections"]


@router.get("/stocks/{ticker}/chart", summary="Plot-ready series: closes, SMAs, projection fan, EPS")
def stock_chart(request: HttpRequest, ticker: str):
    d = queries.chart_payload(ticker)
    if d is None:
        raise HttpError(404, f"Unknown ticker {ticker}")
    return d


@router.get("/stocks/{ticker}/news", response=list[NewsSchema])
def stock_news(request: HttpRequest, ticker: str, label: str = "", limit: int = Query(30, le=200)):
    return queries.news(ticker, label, limit)


@router.get("/news", response=list[NewsSchema], summary="Latest headlines, labelled positive/negative")
def all_news(request: HttpRequest, label: str = "", limit: int = Query(40, le=200)):
    return queries.news(None, label, limit)


@router.get("/movers", response=list[StockRow], summary="Gainers, losers, most active, most volatile")
def movers(
    request: HttpRequest,
    kind: Literal["gainers", "losers", "active", "volatile", "ytd"] = "gainers",
    limit: int = Query(10, le=50),
):
    return queries.movers(kind, limit)


@router.get("/recommendations", response=RecommendationSchema, summary="Top 5 now + risk-profile picks")
def recommendations(request: HttpRequest):
    return queries.recommendations()


@router.get("/recommendations/scatter", response=list[ScatterPoint])
def recommendation_scatter(request: HttpRequest):
    return queries.scatter()


@router.get("/calculator", throttle=CALC_THROTTLE, summary="Build and simulate a portfolio for an amount, risk profile and horizon")
def calculator(
    request: HttpRequest,
    amount: float = Query(100_000, ge=1_000, le=1_000_000_000, description="KES to invest"),
    profile: Literal["low", "steady", "high"] = "steady",
    horizon: float = Query(1, gt=0, le=3650),
    unit: Literal["days", "weeks", "months", "years"] = "years",
    costs: bool = Query(True, description="Include ~2.1% NSE trading costs per side"),
):
    try:
        return queries.calculator(amount, profile, horizon, unit, costs)
    except CalculatorError as exc:
        raise HttpError(400, str(exc))


@router.get("/experiments/{kind}", summary="PCA, eigenvalue spectrum, Markov chain, forecast accuracy, or plain-language notes")
def experiment(request: HttpRequest, kind: Literal["pca", "eigen", "markov", "accuracy", "notes"], ticker: str = "NASI"):
    try:
        return queries.experiment(kind, ticker.upper() if kind == "markov" else "")
    except ExperimentError as exc:
        raise HttpError(400, str(exc))


@router.get(
    "/download/{dataset}",
    throttle=DOWNLOAD_THROTTLE,
    summary="Download a dataset as CSV (Excel) or JSON",
)
def download(
    request: HttpRequest,
    dataset: Literal["stocks", "prices", "fundamentals", "projections", "accuracy", "macro", "news", "picks", "pca", "eigenvalues", "markov", "glossary"],
    format: Literal["csv", "json"] = "csv",
    ticker: str = "",
    start: str = "",
    end: str = "",
    label: str = "",
    series: str = "",
):
    import datetime as dt
    import re

    from .downloads import DATASETS, Params, respond
    from .services.macro import SERIES
    from .services.experiments import ExperimentError as ExpErr

    t = ticker.strip().upper()
    if t and not re.fullmatch(r"[A-Z0-9.\-]{1,16}", t):
        raise HttpError(400, "Invalid ticker")
    try:
        s = dt.date.fromisoformat(start) if start else None
        e = dt.date.fromisoformat(end) if end else None
    except ValueError:
        raise HttpError(400, "Dates must be YYYY-MM-DD")
    if series and series not in SERIES:
        raise HttpError(400, "Unknown series")
    try:
        return respond(DATASETS[dataset], Params(ticker=t, start=s, end=e, label=label, series=series), format)
    except ExpErr as exc:
        raise HttpError(400, str(exc))


@router.get("/glossary", summary="Every term used on the site")
def glossary(request: HttpRequest):
    from .glossary import flat

    return flat()


@router.get("/factors", summary="Macro environment (CBK rates, inflation, shilling, oil), who it helps or hurts, measured sensitivity and company events")
def factors_overview(request: HttpRequest):
    return queries.factors_overview()


@router.get("/stocks/{ticker}/factors", summary="Macro tailwinds/headwinds and recent company events for one stock")
def stock_factors(request: HttpRequest, ticker: str):
    data = queries.stock_factors(ticker[:16])
    if data is None:
        raise HttpError(404, "Unknown ticker")
    return data


@router.get("/jobs", summary="Recent scrape / analysis jobs")
def jobs(request: HttpRequest):
    return queries.job_status()


@router.post("/jobs/refresh", response=JobStarted, auth=JOB_AUTH, summary="Start a background refresh (API key or staff login)")
def refresh(request: HttpRequest, kind: Literal["prices", "news", "analysis", "daily"] = "prices"):
    started = pipeline.start_background(kind)
    return {
        "started": started,
        "kind": kind,
        "message": "started" if started else "a refresh is already running",
    }


api.add_router("/", router)
api.add_router("/ui/", ui_router)
