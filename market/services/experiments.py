"""Quant experiments on NSE daily returns: PCA, eigenvalue spectrum, Markov chains."""

import datetime as dt
import hashlib
import math
from collections import Counter, defaultdict

import numpy as np

from django.db.models import Count

from ..models import PriceBar, Stock, StockAnalytics
from . import catalog
from .ingest import INDEX_TICKER

MAX_JUMP = 0.20  # one-day moves beyond ±20% are corporate actions in the unadjusted history
STATES = ["Big down", "Down", "Flat", "Up", "Big up"]
BIG, FLAT = 1.0, 0.2  # state thresholds in units of the series' daily σ


class ExperimentError(ValueError):
    pass


# ------------------------------------------------------------------ data
def _calendar(days: int) -> list[dt.date]:
    idx = Stock.objects.filter(ticker=INDEX_TICKER).first()
    dates = []
    if idx:
        dates = list(PriceBar.objects.filter(stock=idx).order_by("-date").values_list("date", flat=True)[: days + 1])[::-1]
    if len(dates) < 60:  # fall back to the counter with the longest history
        top = PriceBar.objects.values("stock").annotate(n=Count("id")).order_by("-n").first()
        if not top:
            return []
        dates = list(PriceBar.objects.filter(stock_id=top["stock"]).order_by("-date").values_list("date", flat=True)[: days + 1])[::-1]
    return dates


def return_matrix(days: int = 252, min_coverage: float = 0.9):
    """Aligned daily log returns (T × N) for liquid ordinary shares with enough history."""
    dates = _calendar(days)
    if len(dates) < 60:
        raise ExperimentError("Not enough price history yet.")
    pool = (
        StockAnalytics.objects.select_related("stock")
        .filter(is_liquid=True, stock__kind=Stock.KIND_EQUITY, stock__is_suspended=False)
        .exclude(stock__ticker__in=catalog.NON_EQUITY)
    )
    stocks = [a.stock for a in pool]
    start = dates[0] - dt.timedelta(days=20)
    rows = defaultdict(dict)
    for sid, d, c in PriceBar.objects.filter(stock__in=stocks, date__gte=start).values_list("stock_id", "date", "close"):
        rows[sid][d] = c
    pos = {d: i for i, d in enumerate(dates)}
    cols, meta = [], []
    for st in stocks:
        series = rows.get(st.id, {})
        hits = sum(1 for d in dates if d in series)
        if hits < min_coverage * len(dates):
            continue
        before = [d for d in series if d <= dates[0]]
        last = series[max(before)] if before else None
        px = np.empty(len(dates))
        for d in dates:
            last = series.get(d, last)
            px[pos[d]] = np.nan if last is None else last
        if np.isnan(px).any():
            continue
        r = np.diff(np.log(px))
        r = np.where(np.abs(r) > MAX_JUMP, 0.0, r)
        if r.std() == 0:
            continue
        cols.append(r)
        meta.append({"ticker": st.ticker, "name": st.name, "sector": st.sector or "Other"})
    if len(cols) < 5:
        raise ExperimentError("Need at least five liquid counters with a full year of history.")
    return meta, dates[1:], np.column_stack(cols)


def _eig(C: np.ndarray):
    vals, vecs = np.linalg.eigh(C)
    order = np.argsort(vals)[::-1]
    vals, vecs = vals[order], vecs[:, order]
    for j in range(vecs.shape[1]):  # sign convention: largest |loading| positive (PC1: mostly positive)
        ref = vecs[:, j].sum() if j == 0 else vecs[np.argmax(np.abs(vecs[:, j])), j]
        if ref < 0:
            vecs[:, j] *= -1
    return np.clip(vals, 0, None), vecs


# ------------------------------------------------------------------ PCA
def pca(days: int = 252) -> dict:
    meta, dates, R = return_matrix(days)
    T, N = R.shape
    C = np.corrcoef(R, rowvar=False)
    vals, vecs = _eig(C)
    explained = vals / vals.sum()
    cumulative = np.cumsum(explained)
    loadings = vecs * np.sqrt(vals)[None, :]  # correlation of each stock with each component
    eff_bets = float(vals.sum() ** 2 / (vals ** 2).sum())
    k80 = int(np.searchsorted(cumulative, 0.8) + 1)

    def comp(j):
        order = np.argsort(loadings[:, j])[::-1]
        items = [{**meta[i], "loading": round(float(loadings[i, j]), 4)} for i in order]
        return {"pc": j + 1, "explained": float(explained[j]), "loadings": items,
                "top": [x["ticker"] for x in items[:3]], "bottom": [x["ticker"] for x in items[-3:]][::-1]}

    sectors = Counter(m["sector"] for m in meta).most_common(3)
    top_sectors = [s for s, _ in sectors]
    show = min(10, N)
    return {
        "n_stocks": N,
        "n_days": T,
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "eigenvalues": [round(float(v), 4) for v in vals],
        "explained": [round(float(x), 5) for x in explained[:show]],
        "cumulative": [round(float(x), 5) for x in cumulative[:show]],
        "effective_bets": eff_bets,
        "k80": k80,
        "components": [comp(j) for j in range(min(3, N))],
        "top_sectors": top_sectors,
        "map": [
            {**m, "pc1": round(float(loadings[i, 0]), 4), "pc2": round(float(loadings[i, 1]), 4),
             "group": m["sector"] if m["sector"] in top_sectors else "Other"}
            for i, m in enumerate(meta)
        ],
    }


# ------------------------------------------------------------------ eigenvalues
def eigen(days: int = 252, history_days: int = 756, window: int = 126, step: int = 5) -> dict:
    meta, dates, R = return_matrix(days)
    T, N = R.shape
    vals, _ = _eig(np.corrcoef(R, rowvar=False))
    q = N / T
    lam_plus, lam_minus = (1 + math.sqrt(q)) ** 2, (1 - math.sqrt(q)) ** 2
    signal = int((vals > lam_plus).sum())

    # absorption ratio (Kritzman et al.): share of variance absorbed by the top fifth of eigenvectors
    ar_series = {"dates": [], "ar": []}
    try:
        _, hdates, H = return_matrix(history_days, min_coverage=0.85)
        k = max(1, round(H.shape[1] / 5))
        for end in range(window, H.shape[0] + 1, step):
            w = H[end - window:end]
            v = np.linalg.eigvalsh(np.corrcoef(w, rowvar=False))[::-1]
            ar_series["dates"].append(hdates[end - 1].isoformat())
            ar_series["ar"].append(round(float(v[:k].sum() / v.sum()), 5))
        ar_series["k"] = k
        ar_series["n"] = H.shape[1]
    except ExperimentError:
        pass
    ar = ar_series["ar"]
    ar_now = ar[-1] if ar else None
    return {
        "n_stocks": N,
        "n_days": T,
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "eigenvalues": [round(float(v), 4) for v in vals],
        "tickers_note": N,
        "lambda_plus": lam_plus,
        "lambda_minus": lam_minus,
        "signal": signal,
        "market_mode": float(vals[0]),
        "market_share": float(vals[0] / vals.sum()),
        "absorption": ar_series,
        "ar_now": ar_now,
        "ar_mean": float(np.mean(ar)) if ar else None,
        "ar_percentile": float((np.array(ar) <= ar_now).mean()) if ar else None,
        "ar_change_3m": (ar_now - ar[-13]) if len(ar) > 13 else None,
    }


# ------------------------------------------------------------------ Markov chain
def _chi2_sf(x: float, k: int) -> float:
    """Upper-tail p-value of χ²(k) via the Wilson–Hilferty normal approximation."""
    if k <= 0:
        return 1.0
    z = ((x / k) ** (1 / 3) - (1 - 2 / (9 * k))) / math.sqrt(2 / (9 * k))
    return 0.5 * math.erfc(z / math.sqrt(2))


def markov_tickers() -> list[dict]:
    rows = []
    if PriceBar.objects.filter(stock__ticker=INDEX_TICKER).count() >= 120:
        rows.append({"ticker": INDEX_TICKER, "name": "NSE All Share Index"})
    qs = (
        StockAnalytics.objects.select_related("stock")
        .filter(stock__kind=Stock.KIND_EQUITY, stock__is_suspended=False)
        .exclude(stock__ticker__in=catalog.NON_EQUITY)
        .order_by("stock__ticker")
    )
    rows += [{"ticker": a.stock.ticker, "name": a.stock.name} for a in qs if (a.technicals or {}).get("history_days", 0) >= 260]
    return rows


def markov(ticker: str = INDEX_TICKER, days: int = 756, horizon: int = 21, n_paths: int = 2000) -> dict:
    st = Stock.objects.filter(ticker=ticker.upper()).first()
    if st is None:
        raise ExperimentError(f"Unknown ticker {ticker}.")
    closes = list(PriceBar.objects.filter(stock=st).order_by("-date").values_list("date", "close")[: days + 1])[::-1]
    if len(closes) < 120:
        raise ExperimentError(f"{st.ticker} needs at least 120 days of history.")
    dates = [d for d, _ in closes]
    px = np.array([c for _, c in closes], dtype=float)
    r = np.diff(np.log(px))
    r = np.where(np.abs(r) > MAX_JUMP, 0.0, r)
    sigma = float(r.std()) or 1e-9
    edges = np.array([-BIG, -FLAT, FLAT, BIG]) * sigma
    s = np.digitize(r, edges)  # 0..4
    K = len(STATES)
    counts = np.zeros((K, K))
    for a, b in zip(s[:-1], s[1:]):
        counts[a, b] += 1
    rowsum = counts.sum(axis=1, keepdims=True)
    P = np.divide(counts, rowsum, out=np.full_like(counts, 1 / K), where=rowsum > 0)

    # stationary distribution: left eigenvector of P with eigenvalue 1
    w, v = np.linalg.eig(P.T)
    i1 = int(np.argmin(np.abs(w - 1)))
    pi = np.real(v[:, i1])
    pi = pi / pi.sum()
    mods = np.sort(np.abs(w))[::-1]
    slem = float(mods[1]) if len(mods) > 1 else 0.0
    half_life = (math.log(0.5) / math.log(slem)) if 0 < slem < 1 else None

    # does today's state tell us anything about tomorrow? (χ² test of independence)
    total = counts.sum()
    expected = rowsum @ counts.sum(axis=0, keepdims=True) / total
    mask = expected > 0
    chi2 = float((((counts - expected) ** 2)[mask] / expected[mask]).sum())
    dof = (int((rowsum > 0).sum()) - 1) * (int((counts.sum(axis=0) > 0).sum()) - 1)
    p_value = _chi2_sf(chi2, dof)

    mean_ret = np.array([r[s == k].mean() if (s == k).any() else 0.0 for k in range(K)])
    cur = int(s[-1])
    nxt = P[cur]
    steps = {n: np.linalg.matrix_power(P, n)[cur] for n in (1, 5, 21)}
    up_after_up = float(counts[3:, 3:].sum() / max(counts[3:].sum(), 1))
    up_base = float(pi[3:].sum())

    # Markov-switching bootstrap: walk the chain, draw a historical return from each visited state
    rng = np.random.default_rng(int(hashlib.sha256(f"{st.ticker}{dates[-1]}{len(r)}".encode()).hexdigest()[:8], 16))
    pools = [r[s == k] if (s == k).any() else np.array([0.0]) for k in range(K)]
    cum = np.cumsum(P, axis=1)
    state = np.full(n_paths, cur)
    logp = np.zeros((n_paths, horizon + 1))
    for t in range(1, horizon + 1):
        u = rng.random(n_paths)
        state = np.minimum((u[:, None] > cum[state]).sum(axis=1), K - 1)
        draw = np.empty(n_paths)
        for k in range(K):
            m = state == k
            if m.any():
                draw[m] = rng.choice(pools[k], size=int(m.sum()))
        logp[:, t] = logp[:, t - 1] + draw
    paths = px[-1] * np.exp(logp)
    fan_dates, d = [dates[-1]], dates[-1]
    while len(fan_dates) <= horizon:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            fan_dates.append(d)

    def pctl(q):
        return np.percentile(paths, q, axis=0).round(4).tolist()

    return {
        "ticker": st.ticker,
        "name": st.name,
        "start": dates[0].isoformat(),
        "end": dates[-1].isoformat(),
        "n_days": len(r),
        "states": STATES,
        "sigma": sigma,
        "thresholds": [round(float(x) * 100, 3) for x in edges],
        "counts": counts.astype(int).tolist(),
        "P": P.round(4).tolist(),
        "matrix": [
            {"state": STATES[i], "n": int(rowsum[i, 0]), "cells": [{"p": float(P[i, j]), "n": int(counts[i, j])} for j in range(K)]}
            for i in range(K)
        ],
        "stationary": pi.round(4).tolist(),
        "eigen_moduli": mods.round(4).tolist(),
        "slem": slem,
        "half_life": half_life,
        "chi2": chi2,
        "dof": dof,
        "p_value": p_value,
        "has_memory": p_value < 0.05,
        "current": STATES[cur],
        "current_index": cur,
        "next": nxt.round(4).tolist(),
        "likely_next": STATES[int(np.argmax(nxt))],
        "likely_next_p": float(nxt.max()),
        "p_up_1": float(steps[1][3:].sum()),
        "p_up_5": float(steps[5][3:].sum()),
        "p_up_21": float(steps[21][3:].sum()),
        "expected_next": float(nxt @ mean_ret),
        "mean_returns": (mean_ret * 100).round(3).tolist(),
        "durations": [(1 / (1 - P[k, k])) if P[k, k] < 1 else None for k in range(K)],
        "up_after_up": up_after_up,
        "up_base": up_base,
        "last_price": float(px[-1]),
        "fan": {"dates": [x.isoformat() for x in fan_dates], "p5": pctl(5), "p25": pctl(25), "p50": pctl(50), "p75": pctl(75), "p95": pctl(95)},
    }
