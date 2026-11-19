from typing import Any

from ninja import Schema


class IndexSchema(Schema):
    value: float | None = None
    change: float | None = None
    change_pct: float | None = None
    ret_1y: float | None = None
    as_of: str | None = None


class SummarySchema(Schema):
    as_of: str | None
    analysed_at: str | None
    prices_updated_at: str | None
    index: dict[str, Any]
    breadth: dict[str, int]
    stocks: int
    with_history: int
    headlines: int


class StockRow(Schema):
    ticker: str
    name: str
    sector: str
    price: float | None
    change: float | None
    change_pct: float | None
    volume: int | None
    market_cap: float | None
    pe: float | None
    peg: float | None
    pb: float | None
    ps: float | None
    eps: float | None
    roe: float | None
    gross_margin: float | None
    fcf_yield: float | None
    debt_to_equity: float | None
    dividend_yield: float | None
    payout_ratio: float | None
    vol_1y: float | None
    ret_1m: float | None
    ret_ytd: float | None
    rsi_14: float | None
    avg_turnover: float | None
    exp_1m: float | None
    exp_1y: float | None
    composite: float | None
    risk: float | None
    risk_adjusted: float | None
    sentiment: float | None
    liquid: bool
    suspended: bool
    pillars: dict[str, float | None]


class ProjectionSchema(Schema):
    horizon: str
    label: str
    as_of: str
    target_date: str
    base_price: float
    expected_price: float
    low: float
    high: float
    expected_return: float
    prob_up: float
    components: dict[str, Any]
    backtest: dict[str, Any]


class StockDetail(Schema):
    ticker: str
    name: str
    sector: str
    suspended: bool
    kind: str
    as_of: str | None
    row: StockRow | None
    metrics: dict[str, Any]
    technicals: dict[str, Any]
    scores: dict[str, Any]
    reasons: list[str]
    warnings: list[str]
    projections: list[ProjectionSchema]
    fundamentals_source: str | None
    fundamentals_fetched: str | None


class PricePoint(Schema):
    date: str
    close: float
    volume: int | None


class NewsSchema(Schema):
    ticker: str
    title: str
    url: str
    source: str
    published_at: str
    sentiment: float
    label: str
    terms: list[str]


class PickSchema(Schema):
    ticker: str
    name: str
    sector: str
    price: float | None
    change_pct: float | None
    composite: float
    risk: float
    risk_adjusted: float
    exp_1y: float | None
    exp_1m: float | None
    prob_up_1y: float | None
    vol_1y: float | None
    dividend_yield: float | None
    pe: float | None
    sentiment: float | None
    pillars: dict[str, float | None]
    reasons: list[str]
    warnings: list[str]


class RecommendationSchema(Schema):
    as_of: str | None = None
    generated_at: str | None = None
    top5: list[PickSchema] = []
    high_risk_high_gain: list[PickSchema] = []
    low_risk_low_gain: list[PickSchema] = []
    method: dict[str, Any] = {}


class ScatterPoint(Schema):
    ticker: str
    name: str
    risk: float
    exp_1y: float
    composite: float
    market_cap: float | None
    group: str


class JobStarted(Schema):
    started: bool
    kind: str
    message: str


class ErrorSchema(Schema):
    detail: str
