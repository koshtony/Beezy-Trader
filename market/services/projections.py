"""Price projections for 1 day, 1 week, 1 month and 1 year.

The model is a horizon-weighted blend, deliberately simple and explainable:

* **Statistical drift** – three estimates of the daily log-return drift
  - *recent*: EWMA (half-life 10 sessions) blended with the OLS slope of the last 20 closes
  - *medium*: mean daily return over the last year
  - *long*:   mean daily return over the full history (up to ~10 years)
  Each estimate is shrunk toward zero according to its own t-statistic, so noisy trends do
  not dominate. Short horizons weight recent data most; long horizons weight history most.
* **News tilt** – recent headline sentiment nudges the 1D/1W drift by a fraction of daily vol.
* **Fundamental anchor** (1M, 1Y) – expected EPS growth plus a partial re-rating toward the
  sector median P/E, and the analyst price target where one exists.
* **Uncertainty** – horizon-blended volatility (EWMA λ=0.94 for the short run, 1-year and
  long-run σ for longer horizons); a 90% log-normal band and the probability of a gain.

A walk-forward backtest (statistical component) reports direction hit-rate, error versus a
no-change forecast, and how often the realised price landed inside the 90% band.
"""

import datetime as dt
import math

import numpy as np

HORIZONS = {"1D": 1, "1W": 5, "1M": 21, "1Y": 252}
# (recent, medium, long) weights for the statistical drift
DRIFT_WEIGHTS = {
    "1D": (0.60, 0.30, 0.10),
    "1W": (0.50, 0.30, 0.20),
    "1M": (0.35, 0.35, 0.30),
    "1Y": (0.15, 0.35, 0.50),
}
# (recent ewma, 1y, long-run) weights for volatility
VOL_WEIGHTS = {
    "1D": (0.80, 0.20, 0.00),
    "1W": (0.65, 0.35, 0.00),
    "1M": (0.45, 0.45, 0.10),
    "1Y": (0.20, 0.40, 0.40),
}
# (statistical, fundamental, analyst) weights by horizon
ANCHOR_WEIGHTS = {
    "1D": (1.0, 0.0, 0.0),
    "1W": (1.0, 0.0, 0.0),
    "1M": (0.80, 0.12, 0.08),
    "1Y": (0.45, 0.30, 0.25),
}
Z90 = 1.6448536269514722
MAX_ANNUAL_DRIFT = 0.35  # cap on |statistical drift| (annualised log return)
# NSE daily moves are normally limited to ±10%; bigger one-day jumps in the unadjusted
# history are splits, bonus issues or data errors, so they are excluded from estimates.
MAX_DAILY_MOVE = 0.20


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _shrink(mu: float, sigma: float, n: int) -> float:
    """James–Stein style shrinkage toward 0 based on the estimate's t-stat."""
    if n < 2 or sigma <= 0:
        return 0.0
    t = mu / (sigma / math.sqrt(n))
    return mu * (t * t / (t * t + 1.0))


def _ewma_mean(r: np.ndarray, halflife: float) -> float:
    if len(r) == 0:
        return 0.0
    alpha = 1 - 0.5 ** (1 / halflife)
    w = (1 - alpha) ** np.arange(len(r))[::-1]
    return float((w * r).sum() / w.sum())


def _ewma_vol(r: np.ndarray, lam: float = 0.94) -> float:
    if len(r) == 0:
        return 0.0
    r = r[-250:]
    var = float(r[: min(20, len(r))].var()) if len(r) > 1 else float(r[0] ** 2)
    for x in r:
        var = lam * var + (1 - lam) * x * x
    return math.sqrt(var)


def _ols_slope(y: np.ndarray) -> float:
    if len(y) < 3:
        return 0.0
    x = np.arange(len(y), dtype=float)
    x -= x.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())


def statistical_inputs(close: np.ndarray) -> dict | None:
    """Drift and volatility estimates (per trading day, log-return units)."""
    if len(close) < 30:
        return None
    logp = np.log(close)
    r = np.diff(logp)
    r = np.where(np.abs(r) > MAX_DAILY_MOVE, 0.0, r)
    logp = np.concatenate([[logp[0]], logp[0] + np.cumsum(r)])  # jump-free path for the trend fit
    rec = r[-60:]
    sig_rec = _ewma_vol(r)
    sig_mid = float(r[-252:].std(ddof=1))
    sig_long = float(r[-2520:].std(ddof=1))
    floor = 0.002  # avoid zero vol on illiquid counters
    sig_rec, sig_mid, sig_long = (max(s, floor) for s in (sig_rec, sig_mid, sig_long))

    mu_recent_raw = 0.5 * _ewma_mean(rec, 10) + 0.5 * _ols_slope(logp[-20:])
    mu_mid_raw = float(r[-252:].mean())
    mu_long_raw = float(r[-2520:].mean())
    mu_recent = _shrink(mu_recent_raw, sig_rec, min(len(rec), 20))
    mu_mid = _shrink(mu_mid_raw, sig_mid, min(len(r), 252))
    mu_long = _shrink(mu_long_raw, sig_long, min(len(r), 2520))
    return {
        "mu_recent": mu_recent,
        "mu_mid": mu_mid,
        "mu_long": mu_long,
        "mu_recent_raw": mu_recent_raw,
        "mu_mid_raw": mu_mid_raw,
        "mu_long_raw": mu_long_raw,
        "sig_recent": sig_rec,
        "sig_mid": sig_mid,
        "sig_long": sig_long,
        "n": len(close),
    }


def statistical_drift(inp: dict, horizon: str) -> tuple[float, float]:
    """Per-day drift and per-day sigma for a horizon (statistical component)."""
    wr, wm, wl = DRIFT_WEIGHTS[horizon]
    mu = wr * inp["mu_recent"] + wm * inp["mu_mid"] + wl * inp["mu_long"]
    cap = MAX_ANNUAL_DRIFT / 252
    mu = max(-cap, min(cap, mu))
    vr, vm, vl = VOL_WEIGHTS[horizon]
    sig = math.sqrt(vr * inp["sig_recent"] ** 2 + vm * inp["sig_mid"] ** 2 + vl * inp["sig_long"] ** 2)
    return mu, sig


def fundamental_annual_return(val: dict, sector_pe: float | None) -> tuple[float | None, dict]:
    """Expected 1-year log return implied by earnings growth and P/E re-rating."""
    if not val or not val.get("available"):
        return None, {}
    g = val.get("growth")
    pe = val.get("pe")
    parts = {}
    if g is None or pe is None:
        return None, parts
    g = max(-20.0, min(30.0, g)) / 100.0
    parts["eps_growth"] = g
    rerate = 0.0
    if sector_pe and pe > 0:
        # close one third of the gap to the sector median multiple per year, capped ±15%
        rerate = (sector_pe / pe) ** (1 / 3) - 1
        rerate = max(-0.15, min(0.15, rerate))
    parts["rerating"] = rerate
    return math.log(max(1e-6, (1 + g) * (1 + rerate))), parts


def analyst_annual_return(val: dict, price: float) -> tuple[float | None, float]:
    tgt = (val or {}).get("analyst_target")
    if not tgt or not price:
        return None, 0.0
    ret = max(-0.5, min(0.5, tgt / price - 1))
    count = (val or {}).get("analyst_count") or 1
    confidence = min(1.0, 0.5 + 0.2 * (count - 1))
    return math.log(1 + ret), confidence


def project(
    close: np.ndarray,
    last_date: dt.date,
    *,
    valuation: dict | None = None,
    sector_pe: float | None = None,
    news_score: float = 0.0,
) -> dict:
    """Return {horizon: projection dict} using all available inputs."""
    inp = statistical_inputs(close)
    if inp is None:
        return {}
    price = float(close[-1])
    f_ret, f_parts = fundamental_annual_return(valuation or {}, sector_pe)
    a_ret, a_conf = analyst_annual_return(valuation or {}, price)
    out = {}
    for hz, h in HORIZONS.items():
        mu_s, sig = statistical_drift(inp, hz)
        stat_total = mu_s * h
        # news tilt only for the short run (decays after one week)
        tilt = 0.0
        if hz in ("1D", "1W") and news_score:
            tilt = news_score * 0.15 * inp["sig_recent"] * min(h, 5)

        ws, wf, wa = ANCHOR_WEIGHTS[hz]
        comps = {"statistical": stat_total}
        weights = {"statistical": ws}
        if f_ret is not None and wf:
            comps["fundamental"] = f_ret * h / 252
            weights["fundamental"] = wf
        if a_ret is not None and wa:
            comps["analyst"] = a_ret * h / 252
            weights["analyst"] = wa * a_conf
        wsum = sum(weights.values())
        total = sum(comps[k] * weights[k] for k in comps) / wsum + tilt

        sd = sig * math.sqrt(h)
        expected = price * math.exp(total)
        out[hz] = {
            "horizon": hz,
            "days": h,
            "target_date": add_trading_days(last_date, h).isoformat(),
            "base_price": price,
            "expected_price": expected,
            "low": price * math.exp(total - Z90 * sd),
            "high": price * math.exp(total + Z90 * sd),
            "expected_return": math.exp(total) - 1,
            "prob_up": _phi(total / sd) if sd > 0 else (1.0 if total > 0 else 0.0),
            "components": {
                "statistical_return": math.exp(stat_total) - 1,
                "fundamental_return": (math.exp(comps["fundamental"]) - 1) if "fundamental" in comps else None,
                "analyst_return": (math.exp(comps["analyst"]) - 1) if "analyst" in comps else None,
                "news_tilt": math.exp(tilt) - 1,
                "weights": {k: round(v / wsum, 3) for k, v in weights.items()},
                "sigma_annual": sig * math.sqrt(252),
                "drift_recent_annual": inp["mu_recent"] * 252,
                "drift_1y_annual": inp["mu_mid"] * 252,
                "drift_long_annual": inp["mu_long"] * 252,
                **({"eps_growth": f_parts.get("eps_growth"), "rerating": f_parts.get("rerating")} if f_parts else {}),
            },
        }
    return out


def add_trading_days(d: dt.date, n: int) -> dt.date:
    cur = d
    while n > 0:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            n -= 1
    return cur


def backtest(close: np.ndarray, horizon: str, *, lookback: int | None = None, step: int | None = None) -> dict:
    """Walk-forward test of the statistical component for one horizon."""
    h = HORIZONS[horizon]
    lookback = lookback or {"1D": 250, "1W": 250, "1M": 500, "1Y": 1000}[horizon]
    step = step or {"1D": 2, "1W": 3, "1M": 7, "1Y": 21}[horizon]
    n = len(close)
    start = max(260, n - lookback - h)
    hits = moves = total = inside = 0
    err_model = err_naive = 0.0
    for t in range(start, n - h, step):
        hist = close[: t + 1]
        inp = statistical_inputs(hist)
        if inp is None:
            continue
        mu, sig = statistical_drift(inp, horizon)
        pred = mu * h
        actual = math.log(close[t + h] / close[t])
        sd = sig * math.sqrt(h)
        total += 1
        err_model += abs(actual - pred)
        err_naive += abs(actual)
        if abs(actual) > 1e-9:  # unchanged prices (common on thin NSE counters) can't be called either way
            moves += 1
            hits += int(np.sign(actual) == np.sign(pred))
        if abs(actual - pred) <= Z90 * sd:
            inside += 1
    if total < 10:
        return {}
    return {
        "samples": total,
        "moves": moves,
        "hit_rate": hits / moves if moves else None,  # direction right, among outcomes that moved
        "flat_share": 1 - moves / total,
        "mae": err_model / total,
        "mae_naive": err_naive / total,
        "skill": 1 - (err_model / err_naive) if err_naive > 0 else 0.0,
        "band_coverage": inside / total,
    }
