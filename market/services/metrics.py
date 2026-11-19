"""Valuation, profitability, debt/dividend metrics and price-based technicals."""

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

TRADING_DAYS = 252


# ------------------------------------------------------------------ helpers
def _f(x):
    """Finite float or None."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(x, nd=4):
    x = _f(x)
    return None if x is None else round(x, nd)


def safe_div(a, b):
    a, b = _f(a), _f(b)
    if a is None or b in (None, 0.0):
        return None
    return a / b


# ------------------------------------------------------------------ technicals
@dataclass
class Series:
    dates: np.ndarray  # datetime64[D]
    close: np.ndarray
    volume: np.ndarray  # float, nan where unknown

    @property
    def n(self):
        return len(self.close)


def make_series(rows) -> Series:
    """rows: iterable of (date, close, volume)."""
    rows = list(rows)
    if not rows:
        return Series(np.array([], dtype="datetime64[D]"), np.array([]), np.array([]))
    d = np.array([r[0] for r in rows], dtype="datetime64[D]")
    c = np.array([r[1] for r in rows], dtype=float)
    v = np.array([np.nan if r[2] is None else r[2] for r in rows], dtype=float)
    mask = np.isfinite(c) & (c > 0)
    return Series(d[mask], c[mask], v[mask])


def log_returns(close: np.ndarray) -> np.ndarray:
    if len(close) < 2:
        return np.array([])
    return np.diff(np.log(close))


def period_return(close: np.ndarray, days: int):
    if len(close) <= days:
        return None
    return close[-1] / close[-1 - days] - 1.0


def rsi(close: np.ndarray, period: int = 14):
    if len(close) <= period + 1:
        return None
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = gain[:period].mean()
    avg_l = loss[:period].mean()
    for g, l in zip(gain[period:], loss[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def max_drawdown(close: np.ndarray):
    if len(close) < 2:
        return None
    peak = np.maximum.accumulate(close)
    return float((close / peak - 1.0).min())


def sma(close: np.ndarray, n: int):
    return float(close[-n:].mean()) if len(close) >= n else None


def beta(stock: Series, index: Series | None, window: int = TRADING_DAYS):
    if index is None or index.n < 30 or stock.n < 30:
        return None
    common, si, ii = np.intersect1d(stock.dates, index.dates, return_indices=True)
    if len(common) < 30:
        return None
    s = np.log(stock.close[si][-window - 1 :])
    m = np.log(index.close[ii][-window - 1 :])
    rs, rm = np.diff(s), np.diff(m)
    var = rm.var()
    if var == 0:
        return None
    return float(np.cov(rs, rm, ddof=0)[0, 1] / var)


def technicals(s: Series, index: Series | None = None, today: dt.date | None = None) -> dict:
    c = s.close
    if s.n == 0:
        return {}
    r = log_returns(c)
    r = np.where(np.abs(r) > 0.20, 0.0, r)  # ignore corporate-action jumps in volatility
    last = float(c[-1])
    prev = float(c[-2]) if s.n > 1 else last
    out = {
        "last": last,
        "prev_close": prev,
        "change": last - prev,
        "change_pct": (last / prev - 1.0) * 100 if prev else 0.0,
        "ret_1w": period_return(c, 5),
        "ret_1m": period_return(c, 21),
        "ret_3m": period_return(c, 63),
        "ret_6m": period_return(c, 126),
        "ret_1y": period_return(c, TRADING_DAYS),
        "history_days": int(s.n),
        "first_date": str(s.dates[0]),
        "last_date": str(s.dates[-1]),
    }
    # YTD
    year = (today or dt.date.fromisoformat(str(s.dates[-1]))).year
    start = np.datetime64(f"{year}-01-01")
    prior = np.where(s.dates < start)[0]
    out["ret_ytd"] = last / float(c[prior[-1]]) - 1.0 if len(prior) else None

    out["vol_30d"] = float(r[-30:].std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) >= 20 else None
    out["vol_1y"] = float(r[-TRADING_DAYS:].std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) >= 60 else None
    out["max_drawdown_1y"] = max_drawdown(c[-TRADING_DAYS:])
    out["rsi_14"] = rsi(c[-200:])
    out["sma_20"] = sma(c, 20)
    out["sma_50"] = sma(c, 50)
    out["sma_200"] = sma(c, 200)
    out["vs_sma200"] = (last / out["sma_200"] - 1.0) if out["sma_200"] else None
    yr = c[-TRADING_DAYS:]
    out["high_52w"] = float(yr.max())
    out["low_52w"] = float(yr.min())
    out["from_high"] = last / out["high_52w"] - 1.0
    out["beta"] = beta(s, index)

    # Liquidity: volume only exists for days we scraped; price activity is a proxy otherwise.
    vol = s.volume[-30:]
    known = vol[np.isfinite(vol)]
    if len(known):
        turn = known * c[-30:][np.isfinite(vol)]
        out["avg_volume"] = float(known.mean())
        out["avg_turnover"] = float(turn.mean())
        out["volume_days"] = int(len(known))
        out["volume"] = None if not np.isfinite(s.volume[-1]) else int(s.volume[-1])
    else:
        out["avg_volume"] = out["avg_turnover"] = None
        out["volume_days"] = 0
        out["volume"] = None
    recent = r[-60:]
    out["active_ratio"] = float((np.abs(recent) > 1e-9).mean()) if len(recent) else 0.0
    return {k: _r(v, 6) if isinstance(v, float) else v for k, v in out.items()}


# ------------------------------------------------------------------ fundamentals
def eps_trend(history: list[dict]):
    """CAGR and consistency (share of YoY increases) from annual EPS (TTM excluded)."""
    annual = [h["eps"] for h in history if h.get("period", "").upper() != "TTM" and _f(h.get("eps")) is not None]
    annual = annual[-6:]
    if len(annual) < 2:
        return None, None
    ups = sum(1 for a, b in zip(annual, annual[1:]) if b > a)
    consistency = ups / (len(annual) - 1)
    first, last = annual[0], annual[-1]
    years = len(annual) - 1
    cagr = None
    if first > 0 and last > 0:
        cagr = (last / first) ** (1 / years) - 1
    return cagr, consistency


def valuation(price: float, f, is_financial: bool, is_bank: bool) -> dict:
    """Compute the investor metrics using the live price. ``f`` is a Fundamentals row (or None)."""
    if f is None:
        return {"available": False}
    eps = _f(f.eps_ttm)
    shares = _f(f.shares_outstanding)
    mcap = price * shares if shares else None
    cagr, consistency = eps_trend(f.eps_history or [])

    growth = _f(f.eps_growth_forecast)
    growth_src = "analyst 3Y forecast"
    if growth is None and cagr is not None:
        growth, growth_src = cagr * 100, "historical EPS CAGR"

    growth_capped = False
    if growth is not None and growth > 40:
        # EPS rebounding off a tiny base (e.g. 0.02 → 2.5) isn't a sustainable growth rate
        growth, growth_capped = 40.0, True
    pe = safe_div(price, eps) if eps and eps > 0 else None
    peg = safe_div(pe, growth) if (pe and growth and growth > 0) else None

    bvps = _f(f.book_value_per_share) or safe_div(f.equity, shares)
    pb = safe_div(price, bvps) if bvps and bvps > 0 else None
    ps = safe_div(mcap, f.revenue_ttm) if (mcap and f.revenue_ttm and f.revenue_ttm > 0) else None

    roe = _f(f.src_roe)
    if roe is None:
        x = safe_div(f.net_income_ttm, f.equity)
        roe = x * 100 if x is not None and f.equity and f.equity > 0 else None

    fcf = _f(f.free_cash_flow)
    if fcf is None and f.operating_cash_flow is not None and f.capex is not None:
        fcf = f.operating_cash_flow - f.capex
    fcf_yield = safe_div(fcf, mcap)
    fcf_yield = fcf_yield * 100 if fcf_yield is not None else None

    gm = _f(f.src_gross_margin)
    if gm is None and f.revenue_ttm and f.gross_profit_ttm and f.gross_profit_ttm < f.revenue_ttm:
        gm = f.gross_profit_ttm / f.revenue_ttm * 100

    de = _f(f.src_debt_to_equity)
    if de is None:
        de = safe_div(f.total_debt, f.equity) if f.equity and f.equity > 0 else None

    dps = _f(f.dividend_per_share) or 0.0
    dy = dps / price * 100 if price and dps else 0.0
    payout = dps / eps * 100 if (eps and eps > 0 and dps) else (_f(f.src_payout_ratio) if dps else None)
    cover = eps / dps if (eps and dps) else None

    out = {
        "available": True,
        "price": price,
        "market_cap": mcap,
        "eps": eps,
        "eps_history": f.eps_history or [],
        "eps_cagr": cagr * 100 if cagr is not None else None,
        "eps_consistency": consistency,
        "growth": growth,
        "growth_source": (growth_src + (" (capped at 40%)" if growth_capped else "")) if growth is not None else None,
        "pe": pe,
        "forward_pe": _f(f.src_forward_pe),
        "peg": peg,
        "bvps": bvps,
        "pb": pb,
        "ps": ps,
        "roe": roe,
        "roa": _f(f.src_roa),
        "fcf": fcf,
        "fcf_yield": fcf_yield,
        "ocf": _f(f.operating_cash_flow),
        "capex": _f(f.capex),
        "gross_margin": gm,
        "profit_margin": _f(f.src_profit_margin),
        "debt_to_equity": de,
        "total_debt": _f(f.total_debt),
        "current_ratio": _f(f.src_current_ratio),
        "dps": dps,
        "dividend_yield": dy,
        "payout_ratio": payout,
        "dividend_cover": cover,
        "revenue": _f(f.revenue_ttm),
        "net_income": _f(f.net_income_ttm),
        "analyst_target": _f(f.analyst_target),
        "analyst_consensus": f.analyst_consensus or None,
        "analyst_count": f.analyst_count,
        "fiscal_period": f.fiscal_period,
        "is_financial": is_financial,
        "is_bank": is_bank,
        "loss_making": eps is not None and eps <= 0,
    }
    return {k: (_r(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


# ------------------------------------------------------------------ verdicts
GOOD, FAIR, POOR, NA = "good", "fair", "poor", "na"


def verdicts(m: dict, sector_pe: float | None, sector_pb: float | None) -> dict:
    """Plain-language judgement for each metric (good/fair/poor + note)."""
    v: dict[str, tuple[str, str]] = {}
    if not m.get("available"):
        return {}
    fin, bank = m["is_financial"], m["is_bank"]

    pe = m["pe"]
    if m["loss_making"]:
        v["pe"] = (POOR, "Loss-making: no meaningful P/E")
    elif pe is None:
        v["pe"] = (NA, "No earnings data")
    else:
        rel = f" vs sector median {sector_pe:.1f}" if sector_pe else ""
        if pe <= 7 or (sector_pe and pe <= 0.8 * sector_pe):
            v["pe"] = (GOOD, f"Cheap on earnings{rel}")
        elif pe <= 15 or (sector_pe and pe <= 1.25 * sector_pe):
            v["pe"] = (FAIR, f"Reasonably priced{rel}")
        else:
            v["pe"] = (POOR, f"Expensive / growth priced in{rel}")

    peg = m["peg"]
    if peg is None:
        v["peg"] = (NA, "Needs positive P/E and growth")
    elif peg < 1:
        v["peg"] = (GOOD, f"Below 1 — cheap relative to {m['growth']:.1f}% growth")
    elif peg <= 2:
        v["peg"] = (FAIR, "Growth roughly priced in")
    else:
        v["peg"] = (POOR, "Paying a premium for growth")

    pb = m["pb"]
    if pb is None:
        v["pb"] = (NA, "No book value")
    elif pb < 1:
        trap = m["roe"] is not None and m["roe"] < 8
        v["pb"] = (FAIR if trap else GOOD, "Below book — possible value trap (low ROE)" if trap else "Trading below book value")
    elif pb <= 2.5 or (sector_pb and pb <= sector_pb):
        v["pb"] = (FAIR, "Moderate premium to book")
    else:
        v["pb"] = (POOR if (m["roe"] or 0) < 25 else FAIR, "High premium to book")

    ps = m["ps"]
    if ps is None:
        v["ps"] = (NA, "No revenue data")
    elif bank:
        v["ps"] = (NA, "Less informative for banks")
    elif ps < 1:
        v["ps"] = (GOOD, "Under 1× sales")
    elif ps <= 3:
        v["ps"] = (FAIR, "1–3× sales")
    else:
        v["ps"] = (POOR, "Over 3× sales")

    cagr, cons = m["eps_cagr"], m["eps_consistency"]
    if m["eps"] is None:
        v["eps"] = (NA, "No EPS data")
    elif m["loss_making"]:
        v["eps"] = (POOR, "Negative earnings")
    elif cagr is None or cons is None:
        v["eps"] = (FAIR, "Positive earnings; limited history")
    elif cagr > 5 and cons >= 0.6:
        v["eps"] = (GOOD, f"Steady growth: {cagr:.1f}%/yr, up {cons:.0%} of years")
    elif cagr > 0:
        v["eps"] = (FAIR, f"Growing {cagr:.1f}%/yr but uneven ({cons:.0%} of years up)")
    else:
        v["eps"] = (POOR, f"EPS shrinking {cagr:.1f}%/yr")

    roe = m["roe"]
    if roe is None:
        v["roe"] = (NA, "No equity data")
    elif roe >= 15:
        v["roe"] = (GOOD, "≥15%: strong returns on capital")
    elif roe >= 8:
        v["roe"] = (FAIR, "8–15%: average")
    else:
        v["roe"] = (POOR, "Under 8%: weak")

    if fin:
        v["fcf"] = (NA, "FCF not meaningful for banks/insurers (deposits & float)")
    elif m["fcf"] is None:
        v["fcf"] = (NA, "No cash-flow data")
    elif m["fcf"] > 0:
        fy = m["fcf_yield"]
        v["fcf"] = (GOOD if (fy or 0) >= 5 else FAIR, f"Positive FCF, yield {fy:.1f}%" if fy is not None else "Positive FCF")
    else:
        v["fcf"] = (POOR, "Burning cash (negative FCF)")

    gm = m["gross_margin"]
    if gm is None:
        v["gross_margin"] = (NA, "Not reported (typical for financials)")
    elif gm >= 40:
        v["gross_margin"] = (GOOD, "High margin / pricing power")
    elif gm >= 20:
        v["gross_margin"] = (FAIR, "Moderate margin")
    else:
        v["gross_margin"] = (POOR, "Thin margin")

    de = m["debt_to_equity"]
    if de is None:
        v["debt_to_equity"] = (NA, "No debt data")
    elif bank:
        v["debt_to_equity"] = (GOOD if de <= 1 else FAIR, "Borrowings only — deposits excluded for banks")
    elif de <= 1:
        v["debt_to_equity"] = (GOOD, "Conservative leverage")
    elif de <= 2:
        v["debt_to_equity"] = (FAIR, "Moderate leverage")
    else:
        v["debt_to_equity"] = (POOR, "Above 2.0 — high leverage risk")

    dy = m["dividend_yield"]
    if not dy:
        v["dividend_yield"] = (POOR, "No dividend")
    elif dy >= 6:
        v["dividend_yield"] = (GOOD, "High income yield")
    elif dy >= 3:
        v["dividend_yield"] = (FAIR, "Moderate yield")
    else:
        v["dividend_yield"] = (POOR, "Low yield")

    payout, cover = m["payout_ratio"], m["dividend_cover"]
    if not m["dps"]:
        v["payout_ratio"] = (NA, "No dividend")
    elif payout is None or cover is None:
        v["payout_ratio"] = (NA, "Unknown cover")
    elif cover >= 1.5 and payout <= 75:
        v["payout_ratio"] = (GOOD, f"Well covered ({cover:.1f}× earnings)")
    elif cover >= 1.0:
        v["payout_ratio"] = (FAIR, f"Covered {cover:.1f}× — limited headroom")
    else:
        v["payout_ratio"] = (POOR, "Paying out more than it earns")
    return {k: {"verdict": a, "note": b} for k, (a, b) in v.items()}
