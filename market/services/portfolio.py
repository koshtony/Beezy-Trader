"""Portfolio calculator: build a portfolio for a risk profile and simulate its value over time.

Steps
1. **Select** counters from the latest analysis run (only liquid, profitable, positively projected
   ordinary shares — the same eligibility as the Top 5), per profile:
   - ``low``    – lowest risk score among counters scoring ≥ 45, weighted by inverse volatility
   - ``steady`` – best risk-adjusted scores (the Top-5 logic), weighted by score
   - ``high``   – above-median risk, highest projected 12-month return, weighted by that return
   At most two counters per sector; each weight is kept between 8% and 35%.
2. **Buy** whole shares after NSE trading costs (≈2.1% per side: brokerage, NSE/CMA/CDSC levies).
3. **Simulate** 4,000 correlated price paths, each with its own random error in the projected
   return (±15%/yr, 1 s.d.) because projections are uncertain too. Each counter's median path follows its own 1D/1W/1M/1Y
   projections (interpolated; beyond a year it fades toward the long-run trend and a 7% market
   price anchor, capped at 10%/yr) and its projected
   volatility; co-movement uses the last year's correlation of daily returns.
   Because we pick the counters with the best projections (which are biased upward), each
   pick's projected path is pulled halfway toward a 7%/yr market price return first.
4. **Add dividends** (current dividend per share, accrued pro rata, less 5% withholding tax) and
   subtract selling costs at the end.
"""

import datetime as dt
import hashlib
import math
from collections import defaultdict

import numpy as np

from ..models import Fundamentals, PriceBar, Projection, RecommendationRun, Stock
from . import catalog

FEE_RATE = 0.021  # approx. all-in NSE equity transaction cost per side
DIVIDEND_WHT = 0.05  # withholding tax on dividends for Kenyan resident individuals
TRADING_DAYS_PER_YEAR = 252
CAL_DAYS_PER_YEAR = 365.25
MAX_CAL_DAYS = 3653  # 10 years
N_PATHS = 4000
MIN_W, MAX_W = 0.08, 0.35
# Projections are themselves uncertain: each simulated path draws its own error in the annual
# expected return (correlated across counters), so long horizons widen honestly.
DRIFT_UNCERTAINTY = 0.15
MARKET_ANCHOR = 0.07  # long-run NSE *price* return (dividends are added separately)
MAX_BEYOND = 0.10
# We pick the counters with the *best* projections, and the best-looking estimates are biased
# upward (winner's curse). Pull each pick's projected path halfway toward the market anchor.
SELECTION_SHRINK = 0.5

PROFILES = {
    "low": {
        "label": "Low risk",
        "tagline": "Calm, dividend-paying blue chips",
        "detail": "Lowest-risk qualifying counters, weighted toward the least volatile.",
        "n": 6,
    },
    "steady": {
        "label": "Steady",
        "tagline": "Best balance of value, quality and outlook",
        "detail": "The highest risk-adjusted scores (our Top 5), weighted by score.",
        "n": 5,
    },
    "high": {
        "label": "High risk",
        "tagline": "Bigger projected gains, bigger swings",
        "detail": "Above-median-risk counters with the largest projected 12-month gains.",
        "n": 5,
    },
}
UNITS = {"days": 1.0, "weeks": 7.0, "months": 30.4375, "years": CAL_DAYS_PER_YEAR}
ANCHORS = [("1D", 1), ("1W", 5), ("1M", 21), ("1Y", 252)]


class CalculatorError(ValueError):
    pass


# ------------------------------------------------------------------ selection
def _universe(run: RecommendationRun) -> list[dict]:
    return [
        u for u in run.payload.get("universe", [])
        if u.get("eligible") and (u.get("exp_1y") or 0) > 0 and u["ticker"] not in catalog.NON_EQUITY
    ]


def _cap_sectors(rows, n, per_sector=2):
    out, count = [], defaultdict(int)
    for r in rows:
        if count[r["sector"]] >= per_sector:
            continue
        out.append(r)
        count[r["sector"]] += 1
        if len(out) == n:
            break
    return out


def select(profile: str, universe: list[dict]) -> list[dict]:
    n = PROFILES[profile]["n"]
    risks = sorted(u["risk"] for u in universe)
    median = risks[len(risks) // 2] if risks else 50
    if profile == "low":
        pool = [u for u in universe if u["composite"] >= 45 and u["risk"] <= median]
        if len(pool) < 3:
            pool = [u for u in universe if u["risk"] <= median] or universe
        return _cap_sectors(sorted(pool, key=lambda u: u["risk"]), n)
    if profile == "steady":
        return _cap_sectors(sorted(universe, key=lambda u: u["risk_adjusted"], reverse=True), n)
    pool = sorted([u for u in universe if u["risk"] >= median], key=lambda u: u["exp_1y"], reverse=True)
    chosen = _cap_sectors(pool, n)
    if len(chosen) < n:  # top up from the rest by projected return
        rest = [u for u in sorted(universe, key=lambda u: u["exp_1y"], reverse=True) if u not in chosen]
        chosen = _cap_sectors(chosen + rest, n)
    return chosen


def _clamp_weights(w: np.ndarray) -> np.ndarray:
    w = w / w.sum()
    lo, hi = (MIN_W, MAX_W) if len(w) * MIN_W <= 1 <= len(w) * MAX_W else (0.0, 1.0)
    for _ in range(50):
        w = np.clip(w, lo, hi)
        w = w / w.sum()
        if w.min() >= lo - 1e-9 and w.max() <= hi + 1e-9:
            break
    return w


def weights(profile: str, picks: list[dict]) -> np.ndarray:
    if profile == "low":
        raw = np.array([1.0 / max(p.get("vol_1y") or 0.25, 0.05) for p in picks])
    elif profile == "steady":
        raw = np.array([max(p["risk_adjusted"] - 30, 5.0) for p in picks])
    else:
        raw = np.array([max(p["exp_1y"], 0.02) for p in picks])
    return _clamp_weights(raw.astype(float))


# ------------------------------------------------------------------ model inputs
def _projection_inputs(stocks: list[Stock]) -> dict[str, dict]:
    """Per ticker: anchor log-returns and annual sigmas from the latest projections."""
    out: dict[str, dict] = {}
    for st in stocks:
        last = Projection.objects.filter(stock=st).order_by("-as_of").values_list("as_of", flat=True).first()
        rows = {p.horizon: p for p in Projection.objects.filter(stock=st, as_of=last)} if last else {}
        if "1Y" not in rows:
            continue
        long_run = rows["1Y"].components.get("drift_long_annual") or 0.0
        out[st.ticker] = {
            # beyond a year: fade toward the long-run trend and a market-level anchor, capped
            "beyond": max(-0.10, min(MAX_BEYOND,
                0.25 * math.log(1 + rows["1Y"].expected_return)
                + 0.25 * max(-0.05, min(0.15, long_run))
                + 0.5 * MARKET_ANCHOR)),
            "t": np.array([0] + [d for _, d in ANCHORS], dtype=float),
            "g": np.array([0.0] + [math.log(1 + rows[h].expected_return) for h, _ in ANCHORS]),
            "sig": np.array(
                [rows["1D"].components.get("sigma_annual", 0.3)]
                + [rows[h].components.get("sigma_annual", 0.3) for h, _ in ANCHORS]
            ),
        }
    return out


def _median_logret(inp: dict, t: np.ndarray) -> np.ndarray:
    tc = np.minimum(t, 252)
    anchor = math.log(1 + MARKET_ANCHOR) * tc / 252
    g = SELECTION_SHRINK * np.interp(tc, inp["t"], inp["g"]) + (1 - SELECTION_SHRINK) * anchor
    beyond = np.maximum(t - 252, 0)
    return g + inp["beyond"] * beyond / 252


def _sigma_annual(inp: dict, t: np.ndarray) -> np.ndarray:
    return np.interp(np.minimum(t, 252), inp["t"], inp["sig"])


def _correlation(stocks: list[Stock]) -> np.ndarray:
    k = len(stocks)
    since = dt.date.today() - dt.timedelta(days=400)
    series = {}
    for st in stocks:
        rows = PriceBar.objects.filter(stock=st, date__gte=since).order_by("date").values_list("date", "close")
        series[st.ticker] = dict(rows)
    common = sorted(set.intersection(*(set(s) for s in series.values()))) if series else []
    if len(common) < 40:
        return np.eye(k)
    mat = np.array([[series[st.ticker][d] for d in common] for st in stocks], dtype=float)
    r = np.diff(np.log(mat), axis=1)
    r = np.where(np.abs(r) > 0.2, 0.0, r)
    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.corrcoef(r)
    c = np.nan_to_num(c, nan=0.0)
    np.fill_diagonal(c, 1.0)
    # nearest positive-definite matrix (clip eigenvalues) so Cholesky always works
    vals, vecs = np.linalg.eigh(c)
    c = vecs @ np.diag(np.clip(vals, 1e-6, None)) @ vecs.T
    d = np.sqrt(np.diag(c))
    return c / np.outer(d, d)


# ------------------------------------------------------------------ simulation
def horizon_days(horizon: float, unit: str) -> tuple[int, int]:
    if unit not in UNITS:
        raise CalculatorError(f"unit must be one of {', '.join(UNITS)}")
    cal = int(round(horizon * UNITS[unit]))
    if cal < 1 or cal > MAX_CAL_DAYS:
        raise CalculatorError("Horizon must be between 1 day and 10 years.")
    trading = max(1, int(round(cal * TRADING_DAYS_PER_YEAR / CAL_DAYS_PER_YEAR)))
    return cal, trading


def _seed(*parts) -> int:
    return int(hashlib.sha256(repr(parts).encode()).hexdigest()[:8], 16)


def simulate(amount: float, profile: str, horizon: float, unit: str, *, include_costs: bool = True,
             run: RecommendationRun | None = None, n_paths: int = N_PATHS, with_paths: bool = True) -> dict:
    if profile not in PROFILES:
        raise CalculatorError(f"profile must be one of {', '.join(PROFILES)}")
    if not (1_000 <= amount <= 1_000_000_000):
        raise CalculatorError("Amount must be between KES 1,000 and KES 1,000,000,000.")
    cal_days, T = horizon_days(horizon, unit)
    run = run or RecommendationRun.objects.order_by("-created_at").first()
    if run is None:
        raise CalculatorError("No analysis has been run yet.")
    picks = select(profile, _universe(run))
    stocks_by = {s.ticker: s for s in Stock.objects.filter(ticker__in=[p["ticker"] for p in picks])}
    inputs = _projection_inputs(list(stocks_by.values()))
    picks = [p for p in picks if p["ticker"] in inputs and p.get("price")]
    if not picks:
        raise CalculatorError("Not enough analysed counters for this profile yet.")
    w = weights(profile, picks)
    fee = FEE_RATE if include_costs else 0.0
    dps = {f.stock.ticker: (f.dividend_per_share or 0.0) for f in Fundamentals.objects.filter(stock__ticker__in=[p["ticker"] for p in picks]).select_related("stock")}

    prices = np.array([p["price"] for p in picks], dtype=float)
    alloc = amount * w
    shares = np.floor(alloc / (prices * (1 + fee)))
    buy_cost = shares * prices * fee
    invested = shares * prices
    cash = amount - (invested + buy_cost).sum()
    years = cal_days / CAL_DAYS_PER_YEAR
    div_per_year = np.array([dps.get(p["ticker"], 0.0) for p in picks]) * shares * (1 - DIVIDEND_WHT)

    # time grid (trading days) — ≤120 points for the chart
    steps = min(T, 120)
    grid = np.unique(np.round(np.linspace(0, T, steps + 1)).astype(int))
    dt_ = np.diff(grid).astype(float)
    k = len(picks)
    corr = _correlation([stocks_by[p["ticker"]] for p in picks])
    chol = np.linalg.cholesky(corr)
    rng = np.random.default_rng(_seed(amount, profile, horizon, unit, include_costs, run.pk))
    z = rng.standard_normal((n_paths, len(dt_), k)) @ chol.T
    drift_err = (rng.standard_normal((n_paths, k)) @ chol.T) * DRIFT_UNCERTAINTY  # per path, annual
    W = np.concatenate([np.zeros((n_paths, 1, k)), np.cumsum(z * np.sqrt(dt_)[None, :, None], axis=1)], axis=1)
    tt = grid.astype(float)
    mu = np.stack([_median_logret(inputs[p["ticker"]], tt) for p in picks], axis=1)  # (steps, k)
    sig_d = np.stack([_sigma_annual(inputs[p["ticker"]], tt) for p in picks], axis=1) / math.sqrt(252)
    logp = (np.log(prices)[None, None, :] + mu[None] + sig_d[None] * W
            # the projection error mostly concerns the first year; later years are anchored
            + drift_err[:, None, :] * ((np.minimum(tt, 252) + 0.5 * np.maximum(tt - 252, 0)) / 252)[None, :, None])
    px = np.exp(logp)

    cal_grid = tt * CAL_DAYS_PER_YEAR / TRADING_DAYS_PER_YEAR
    divs_t = (cal_grid / CAL_DAYS_PER_YEAR)[:, None] * div_per_year[None, :]  # (steps, k)
    mtm = (px * shares[None, None, :]).sum(axis=2) + cash + divs_t.sum(axis=1)[None, :]  # mark-to-market
    stock_val_T = px[:, -1, :] * shares[None, :]
    final = stock_val_T.sum(axis=1) * (1 - fee) + cash + divs_t[-1].sum()

    def pct(a, q):
        return float(np.percentile(a, q))

    today = dt.date.today()
    dates = [(today + dt.timedelta(days=float(c))).isoformat() for c in cal_grid]
    summary = {
        "amount": amount,
        "invested": float(invested.sum()),
        "buy_costs": float(buy_cost.sum()),
        "cash_left": float(cash),
        "dividends": float(divs_t[-1].sum()),
        "median": pct(final, 50),
        "mean": float(final.mean()),
        "p5": pct(final, 5),
        "p25": pct(final, 25),
        "p75": pct(final, 75),
        "p95": pct(final, 95),
        "prob_profit": float((final > amount).mean()),
        "prob_loss_10": float((final < amount * 0.9).mean()),
    }
    summary["profit_median"] = summary["median"] - amount
    summary["return_median"] = summary["median"] / amount - 1
    summary["annualised"] = (summary["median"] / amount) ** (1 / years) - 1 if years >= 1 / 12 else None

    # milestones on the liquidation basis (sell everything, pay costs)
    marks = [7, 30, 91, 182, 365, 730, 1095, 1826, 3652]
    ms = sorted({m for m in marks if m < cal_days * 0.93} | {cal_days})
    milestones = []
    for m in ms:
        i = int(np.argmin(np.abs(cal_grid - m)))
        liq = (px[:, i, :] * shares[None, :]).sum(axis=1) * (1 - fee) + cash + divs_t[i].sum()
        milestones.append({
            "days": m,
            "label": _label_days(m),
            "date": (today + dt.timedelta(days=m)).isoformat(),
            "median": pct(liq, 50),
            "p5": pct(liq, 5),
            "p95": pct(liq, 95),
            "profit": pct(liq, 50) - amount,
            "prob_profit": float((liq > amount).mean()),
        })

    holdings = []
    for j, p in enumerate(picks):
        exp_px = float(np.median(px[:, -1, j]))
        holdings.append({
            "ticker": p["ticker"],
            "name": p["name"],
            "sector": p["sector"],
            "price": float(prices[j]),
            "weight": float(w[j]),
            "shares": int(shares[j]),
            "cost": float(invested[j] + buy_cost[j]),
            "expected_price": exp_px,
            "expected_value": exp_px * shares[j],
            "dividends": float(divs_t[-1][j]),
            "exp_1y": p["exp_1y"],
            "risk": p["risk"],
            "vol_1y": p.get("vol_1y"),
            "dividend_yield": p.get("dividend_yield"),
            "reason": (p.get("reasons") or [""])[0],
        })

    out = {
        "profile": profile,
        "profile_label": PROFILES[profile]["label"],
        "profile_detail": PROFILES[profile]["detail"],
        "amount": amount,
        "horizon": horizon,
        "unit": unit,
        "calendar_days": cal_days,
        "trading_days": T,
        "horizon_label": _label_days(cal_days),
        "include_costs": include_costs,
        "fee_rate": fee,
        "as_of": run.as_of.isoformat() if run.as_of else None,
        "summary": summary,
        "milestones": milestones,
        "holdings": holdings,
        "extrapolated": cal_days > 366,
    }
    if with_paths:
        out["chart"] = {
            "dates": dates,
            "p5": np.percentile(mtm, 5, axis=0).round(2).tolist(),
            "p25": np.percentile(mtm, 25, axis=0).round(2).tolist(),
            "p50": np.percentile(mtm, 50, axis=0).round(2).tolist(),
            "p75": np.percentile(mtm, 75, axis=0).round(2).tolist(),
            "p95": np.percentile(mtm, 95, axis=0).round(2).tolist(),
            "amount": amount,
        }
    return out


def compare(amount: float, horizon: float, unit: str, *, include_costs: bool = True) -> list[dict]:
    """Quick side-by-side of all three profiles for the same inputs."""
    run = RecommendationRun.objects.order_by("-created_at").first()
    rows = []
    for key in PROFILES:
        try:
            r = simulate(amount, key, horizon, unit, include_costs=include_costs, run=run, n_paths=1500, with_paths=False)
        except CalculatorError:
            continue
        s = r["summary"]
        rows.append({
            "profile": key,
            "label": PROFILES[key]["label"],
            "tagline": PROFILES[key]["tagline"],
            "median": s["median"],
            "profit": s["profit_median"],
            "p5": s["p5"],
            "p95": s["p95"],
            "prob_profit": s["prob_profit"],
            "tickers": [h["ticker"] for h in r["holdings"]],
        })
    return rows


def _label_days(d: int) -> str:
    if d < 14:
        return f"{d} day{'s' if d != 1 else ''}"
    if d < 60:
        w = round(d / 7)
        return f"{w} weeks" if abs(d - w * 7) <= 1 else f"{d} days"
    if d < 365:
        m = round(d / 30.4375)
        return f"{m} months"
    y = d / CAL_DAYS_PER_YEAR
    return f"{y:.0f} year{'s' if round(y) != 1 else ''}" if abs(y - round(y)) < 0.05 else f"{y:.1f} years"
