"""Turn metrics, projections, technicals and news into pillar scores, risk and picks.

All scores are 0–100 (higher is better), except ``risk`` where higher means riskier.
"""

import math
import statistics

PILLAR_WEIGHTS = {
    "value": 0.20,
    "quality": 0.20,
    "health": 0.10,
    "income": 0.12,
    "outlook": 0.25,
    "sentiment": 0.13,
}
RISK_PENALTY = 0.20  # composite points lost per risk point above 50
UNKNOWN = 40.0  # score used when a pillar has no data (mild penalty for opacity)


def lower_better(x, good, bad):
    if x is None:
        return None
    if x <= good:
        return 100.0
    if x >= bad:
        return 0.0
    return 100.0 * (bad - x) / (bad - good)


def higher_better(x, bad, good):
    if x is None:
        return None
    if x >= good:
        return 100.0
    if x <= bad:
        return 0.0
    return 100.0 * (x - bad) / (good - bad)


def weighted(parts: list[tuple[float | None, float]]):
    num = den = 0.0
    for s, w in parts:
        if s is None or w <= 0:
            continue
        num += s * w
        den += w
    return num / den if den else None


def median(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return statistics.median(vals) if vals else None


def sector_medians(rows: list[dict]) -> dict:
    """rows: [{'sector':..., 'val': {...}}] -> {sector: {'pe':..., 'pb':..., 'ps':...}}"""
    by: dict[str, dict[str, list]] = {}
    for r in rows:
        v = r.get("val") or {}
        if not v.get("available"):
            continue
        d = by.setdefault(r["sector"], {"pe": [], "pb": [], "ps": []})
        for k in d:
            x = v.get(k)
            if x is not None and 0 < x < 200:
                d[k].append(x)
    market = {k: median([x for d in by.values() for x in d[k]]) for k in ("pe", "pb", "ps")}
    out = {}
    for sec, d in by.items():
        out[sec] = {k: (median(d[k]) if len(d[k]) >= 2 else market[k]) for k in d}
    out["__market__"] = market
    return out


def news_aggregate(articles, now) -> dict:
    """Recency-weighted sentiment (half-life 7 days) with a confidence factor."""
    num = den = 0.0
    pos = neg = neu = 0
    recent7 = []
    for a in articles:
        age = max(0.0, (now - a.published_at).total_seconds() / 86400)
        if age > 30:
            continue
        w = 0.5 ** (age / 7)
        num += w * a.sentiment
        den += w
        if a.label == "positive":
            pos += 1
        elif a.label == "negative":
            neg += 1
        else:
            neu += 1
        if age <= 7:
            recent7.append(a.sentiment)
    raw = num / den if den else 0.0
    confidence = 1 - math.exp(-den / 2.0)
    adj = raw * confidence
    return {
        "score": adj,  # -1..1
        "raw": raw,
        "confidence": confidence,
        "positive": pos,
        "negative": neg,
        "neutral": neu,
        "recent7": sum(recent7) / len(recent7) if recent7 else 0.0,
    }


def score_stock(val: dict, tech: dict, proj: dict, news: dict, medians: dict, *, is_financial: bool, is_bank: bool) -> dict:
    m = val if val.get("available") else {}
    sec_pe, sec_pb = medians.get("pe"), medians.get("pb")

    # ---- value
    pe = m.get("pe")
    if m.get("loss_making"):
        pe_s = 0.0
    else:
        pe_abs = lower_better(pe, 5, 22)
        pe_rel = lower_better(pe / sec_pe, 0.6, 1.6) if (pe and sec_pe) else None
        pe_s = weighted([(pe_abs, 0.5), (pe_rel, 0.5)])
    peg_s = lower_better(m.get("peg"), 0.5, 2.0)
    pb_s = lower_better(m.get("pb"), 0.6, 3.0)
    if pb_s is not None and m.get("pb", 9) < 1 and (m.get("roe") or 0) < 8:
        pb_s *= 0.5  # value trap
    ps_s = None if is_bank else lower_better(m.get("ps"), 0.8, 5.0)
    value = weighted([(pe_s, 0.35), (peg_s, 0.2), (pb_s, 0.4 if is_financial else 0.3), (ps_s, 0.0 if is_financial else 0.15)])

    # ---- quality (profitability & cash flow)
    roe_s = higher_better(m.get("roe"), 0, 25)
    gm_s = None if is_financial else higher_better(m.get("gross_margin"), 10, 50)
    fcf_s = None
    if not is_financial and m.get("fcf") is not None:
        fcf_s = higher_better(m.get("fcf_yield"), -5, 10)
    cagr_s = higher_better(min(m["eps_cagr"], 40) if m.get("eps_cagr") is not None else None, -10, 15)
    cons_s = None if m.get("eps_consistency") is None else 100 * m["eps_consistency"]
    if m.get("loss_making"):
        cagr_s = 0.0 if cagr_s is None else min(cagr_s, 20.0)
    quality = weighted([(roe_s, 0.35), (gm_s, 0.15), (fcf_s, 0.2), (cagr_s, 0.15), (cons_s, 0.15)])

    # ---- health (debt & risk on the balance sheet)
    de = m.get("debt_to_equity")
    de_s = lower_better(de, 0.3, 2.5)
    cr_s = None if is_financial else higher_better(m.get("current_ratio"), 0.6, 1.5)
    earn_s = None if not m else (0.0 if m.get("loss_making") else 100.0)
    health = weighted([(de_s, 0.25 if is_bank else 0.5), (cr_s, 0.2), (earn_s, 0.3)])

    # ---- income
    dy_s = higher_better(m.get("dividend_yield"), 0, 9) if m else None
    cover = m.get("dividend_cover")
    if m.get("dps"):
        if cover is None:
            cov_s = 50.0
        elif cover >= 1.5:
            cov_s = 100.0
        elif cover >= 1.0:
            cov_s = 50.0 + 100 * (cover - 1.0)
        else:
            cov_s = max(0.0, 50.0 * cover)
    else:
        cov_s = 0.0 if m else None
    income = weighted([(dy_s, 0.6), (cov_s, 0.4)])

    # ---- outlook (projections + trend)
    p1y, p1m = proj.get("1Y"), proj.get("1M")
    er1y = p1y["expected_return"] if p1y else None
    er1m = p1m["expected_return"] if p1m else None
    r1y_s = higher_better(er1y * 100 if er1y is not None else None, -15, 30)
    r1m_s = higher_better(er1m * 100 if er1m is not None else None, -5, 6)
    pu_s = higher_better(p1y["prob_up"] if p1y else None, 0.35, 0.80)
    trend_s = higher_better((tech.get("vs_sma200") or 0) * 100 if tech.get("vs_sma200") is not None else None, -15, 15)
    r = tech.get("rsi_14")
    if r is None:
        rsi_s = None
    elif r > 75:
        rsi_s = 20.0
    elif r < 30:
        rsi_s = 65.0
    else:
        rsi_s = 80.0
    outlook = weighted([(r1y_s, 0.45), (r1m_s, 0.2), (pu_s, 0.15), (trend_s, 0.1), (rsi_s, 0.1)])

    # ---- sentiment
    sentiment = 50 + 50 * news.get("score", 0.0)

    scores = {
        "value": value,
        "quality": quality,
        "health": health,
        "income": income,
        "outlook": outlook,
        "sentiment": sentiment,
    }
    composite = sum((scores[k] if scores[k] is not None else UNKNOWN) * w for k, w in PILLAR_WEIGHTS.items())

    # ---- risk (higher = riskier)
    vol = tech.get("vol_1y") or tech.get("vol_30d")
    vol_r = higher_better(vol * 100 if vol else None, 15, 60)
    dd = tech.get("max_drawdown_1y")
    dd_r = higher_better(-dd * 100 if dd is not None else None, 10, 50)
    beta_r = higher_better(tech.get("beta"), 0.5, 1.5)
    turnover = tech.get("avg_turnover")
    # KES 10M+/day is comfortably liquid on the NSE; under KES 100k is very thin.
    liq_good = higher_better(math.log10(turnover) if turnover else None, 5.0, 7.0)
    liq_r = None if liq_good is None else 100 - liq_good
    act = tech.get("active_ratio")
    act_good = higher_better(act, 0.3, 0.85)
    act_r = None if act_good is None else 100 - act_good
    illiq = weighted([(liq_r, 0.5), (act_r, 0.5)])
    de_r = None if is_bank else higher_better(de, 0.5, 3.0)
    earn_r = 100.0 if m.get("loss_making") else (0.0 if m else 50.0)
    risk = weighted([(vol_r, 0.35), (dd_r, 0.2), (beta_r, 0.1), (illiq, 0.15), (de_r, 0.1), (earn_r, 0.1)]) or 50.0

    adjusted = composite - RISK_PENALTY * (risk - 50)
    return {
        "pillars": {k: (round(v, 1) if v is not None else None) for k, v in scores.items()},
        "composite": round(composite, 2),
        "risk": round(risk, 2),
        "risk_adjusted": round(adjusted, 2),
        "detail": {
            "pe": pe_s, "peg": peg_s, "pb": pb_s, "ps": ps_s, "roe": roe_s, "gross_margin": gm_s,
            "fcf": fcf_s, "eps_cagr": cagr_s, "eps_consistency": cons_s, "debt_to_equity": de_s,
            "dividend_yield": dy_s, "dividend_cover": cov_s, "exp_1y": r1y_s, "exp_1m": r1m_s,
            "prob_up": pu_s, "trend": trend_s, "rsi": rsi_s,
            "risk_vol": vol_r, "risk_drawdown": dd_r, "risk_beta": beta_r, "risk_liquidity": illiq,
        },
    }


def is_liquid(tech: dict, min_turnover: float) -> bool:
    turnover = tech.get("avg_turnover")
    active = tech.get("active_ratio") or 0
    if turnover is not None and (tech.get("volume_days") or 0) >= 3:
        return turnover >= min_turnover and active >= 0.35
    # Until we've scraped a few sessions of volume, lean on price activity, but reject
    # counters whose latest session turnover was tiny.
    if turnover is not None and turnover < 0.2 * min_turnover:
        return False
    return active >= 0.5


def explain(ticker: str, val: dict, tech: dict, proj: dict, news: dict, medians: dict, scored: dict) -> tuple[list[str], list[str]]:
    """Human-readable reasons (strengths) and warnings for a stock."""
    reasons, warnings = [], []
    m = val if val.get("available") else {}
    if m:
        pe, sec_pe = m.get("pe"), medians.get("pe")
        if pe and sec_pe and pe < 0.85 * sec_pe:
            reasons.append(f"P/E {pe:.1f} vs sector {sec_pe:.1f}")
        if m.get("peg") is not None and m["peg"] < 1:
            reasons.append(f"PEG {m['peg']:.2f} (<1)")
        if m.get("pb") is not None and m["pb"] < 1 and (m.get("roe") or 0) >= 10:
            reasons.append(f"Below book (P/B {m['pb']:.2f}) with ROE {m['roe']:.0f}%")
        if (m.get("roe") or 0) >= 15:
            reasons.append(f"ROE {m['roe']:.0f}%")
        if m.get("dividend_yield", 0) >= 6 and (m.get("dividend_cover") or 0) >= 1.2:
            reasons.append(f"Yield {m['dividend_yield']:.1f}%, covered {m['dividend_cover']:.1f}×")
        if m.get("eps_cagr") is not None and m["eps_cagr"] > 60 and (m.get("eps_consistency") or 0) >= 0.6:
            reasons.append("EPS recovering strongly from a low base")
        elif m.get("eps_cagr") is not None and m["eps_cagr"] > 8 and (m.get("eps_consistency") or 0) >= 0.6:
            reasons.append(f"EPS growing {m['eps_cagr']:.0f}%/yr")
        if not m.get("is_financial") and (m.get("fcf_yield") or 0) >= 6:
            reasons.append(f"FCF yield {m['fcf_yield']:.1f}%")
        if m.get("loss_making"):
            warnings.append("Loss-making (negative EPS)")
        if not m.get("is_bank") and (m.get("debt_to_equity") or 0) > 2:
            warnings.append(f"High leverage (D/E {m['debt_to_equity']:.1f})")
        if not m.get("is_financial") and m.get("fcf") is not None and m["fcf"] < 0:
            warnings.append("Negative free cash flow")
        if m.get("dps") and (m.get("dividend_cover") or 9) < 1:
            warnings.append("Dividend not covered by earnings")
        if m.get("pb") is not None and m["pb"] < 1 and (m.get("roe") or 0) < 8:
            warnings.append("Possible value trap (cheap on book, weak ROE)")
    else:
        warnings.append("No fundamental data")
    p1y = proj.get("1Y")
    if p1y and p1y["expected_return"] > 0.08:
        reasons.append(f"Projected {p1y['expected_return']:+.0%} in 12 months (P(up) {p1y['prob_up']:.0%})")
    elif p1y and p1y["expected_return"] < -0.05:
        warnings.append(f"Projected {p1y['expected_return']:+.0%} in 12 months")
    if news.get("positive", 0) + news.get("negative", 0) >= 2:
        if news["score"] > 0.15:
            reasons.append(f"Positive news flow ({news['positive']}↑ / {news['negative']}↓)")
        elif news["score"] < -0.15:
            warnings.append(f"Negative news flow ({news['negative']}↓ / {news['positive']}↑)")
    r = tech.get("rsi_14")
    if r is not None and r > 75:
        warnings.append(f"Overbought (RSI {r:.0f})")
    elif r is not None and r < 30:
        reasons.append(f"Oversold bounce potential (RSI {r:.0f})")
    if (tech.get("vol_1y") or 0) > 0.5:
        warnings.append(f"Very volatile ({tech['vol_1y']:.0%} annualised)")
    if (tech.get("history_days") or 0) < 250:
        warnings.append("Short price history — projections less reliable")
    return reasons[:6], warnings[:5]


def pick(rows: list[dict], n: int = 5, per_sector: int = 2) -> list[dict]:
    """Top-n by risk-adjusted score with sector diversification."""
    chosen, count = [], {}
    for r in sorted(rows, key=lambda x: x["risk_adjusted"], reverse=True):
        if count.get(r["sector"], 0) >= per_sector:
            continue
        chosen.append(r)
        count[r["sector"]] = count.get(r["sector"], 0) + 1
        if len(chosen) == n:
            break
    return chosen
