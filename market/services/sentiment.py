"""Finance-aware headline sentiment.

A compact lexicon in the spirit of Loughran–McDonald, tuned for Kenyan market headlines,
with multi-word phrases (checked first), simple negation and a few contextual flips
(e.g. "loss narrows" is good news; "profit warning" is bad news).
Returns a score in [-1, 1] and a label: positive (green), negative (red) or neutral.
"""

import math
import re

# Phrases are matched before single words and consume the words they cover.
PHRASES: dict[str, float] = {
    "profit warning": -2.5,
    "loss narrows": 1.5,
    "narrows loss": 1.5,
    "narrowed loss": 1.5,
    "loss widens": -2.0,
    "widens loss": -2.0,
    "net loss": -1.5,
    "pre-tax loss": -1.5,
    "returns to profit": 2.5,
    "back to profit": 2.5,
    "swings to profit": 2.5,
    "swings to loss": -2.5,
    "record profit": 2.5,
    "record high": 2.0,
    "record low": -2.0,
    "all-time high": 2.0,
    "52-week high": 1.5,
    "52-week low": -1.5,
    "interim dividend": 1.5,
    "final dividend": 1.5,
    "special dividend": 2.0,
    "bonus issue": 1.5,
    "bonus shares": 1.5,
    "share buyback": 1.5,
    "dividend cut": -2.0,
    "skips dividend": -2.0,
    "no dividend": -2.0,
    "suspends dividend": -2.0,
    "rights issue": -0.5,
    "trading suspension": -2.0,
    "suspended from trading": -2.0,
    "under receivership": -3.0,
    "placed under": -1.5,
    "tax demand": -1.5,
    "tax dispute": -1.5,
    "court case": -1.0,
    "court ruling": -0.5,
    "credit rating upgrade": 2.0,
    "credit rating downgrade": -2.0,
    "price target raised": 1.5,
    "price target cut": -1.5,
    "job cuts": -1.5,
    "staff layoffs": -1.5,
    "market share": 0.5,
    "foreign investors exit": -1.5,
    "foreign inflows": 1.0,
    "foreign outflows": -1.0,
    "beats estimates": 2.0,
    "misses estimates": -2.0,
    "higher dividend": 2.0,
    "lower dividend": -1.5,
    "strong demand": 1.5,
    "weak demand": -1.5,
    "cost cutting": 0.5,
    "debt restructuring": -1.0,
    "going concern": -2.5,
    "break even": 1.0,
    "breaks even": 1.5,
    "profit after tax": 1.0,
    "after tax": 0.0,
    "interest rates fall": 0.0,
    "rates fall": 0.0,
    "demand answers": -1.2,
    "planned blackout": -0.8,
    "power outage": -1.2,
    "in limbo": -1.5,
    "up to": 0.0,
    "hit record high": 0.5,
}

# "<rise-verb> ... 12.5%" and "<fall-verb> ... 12.5%" — earnings/price moves with a number.
_PCT = r"[^.;]{0,25}?\d[\d.,]*\s*(?:%|per\s?cent|percent)"
UP_MOVE = re.compile(r"\b(?:up|rises?|rose|jumps?|jumped|grows?|grew|increases?|increased|surges?|surged|climbs?|climbed|soars?|soared|gains?|gained|higher)\b" + _PCT)
DOWN_MOVE = re.compile(r"\b(?:down|falls?|fell|drops?|dropped|declines?|declined|slumps?|slumped|dips?|dipped|plunges?|plunged|tumbles?|lower|shrinks?|shrank)\b" + _PCT)

WORDS: dict[str, float] = {
    # positive
    "profit": 1.0, "profits": 1.0, "profitable": 1.5, "growth": 1.0, "grows": 1.0, "grew": 1.0,
    "rise": 1.0, "rises": 1.0, "rose": 1.0, "rising": 0.8, "gain": 1.0, "gains": 1.0, "gained": 1.0,
    "surge": 2.0, "surges": 2.0, "surged": 2.0, "soar": 2.0, "soars": 2.0, "soared": 2.0,
    "jump": 1.5, "jumps": 1.5, "jumped": 1.5, "rally": 1.5, "rallies": 1.5, "climb": 1.0,
    "climbs": 1.0, "up": 0.3, "higher": 0.8, "increase": 0.8, "increases": 0.8, "increased": 0.8,
    "boost": 1.5, "boosts": 1.5, "boosted": 1.5, "strong": 1.0, "stronger": 1.2, "robust": 1.2,
    "record": 1.0, "dividend": 0.8, "dividends": 0.8, "payout": 0.5, "upgrade": 1.5,
    "upgrades": 1.5, "upgraded": 1.5, "outperform": 1.5, "outperforms": 1.5, "beat": 1.0,
    "beats": 1.5, "expand": 1.0, "expands": 1.0, "expansion": 1.0, "launch": 0.6, "launches": 0.6,
    "partnership": 0.8, "deal": 0.4, "acquire": 0.5, "acquires": 0.5, "acquisition": 0.4,
    "approve": 0.8, "approves": 0.8, "approval": 0.8, "approved": 0.8, "win": 1.2, "wins": 1.2,
    "award": 1.0, "awarded": 1.0, "recover": 1.2, "recovers": 1.2, "recovery": 1.2,
    "rebound": 1.5, "rebounds": 1.5, "turnaround": 1.5, "upbeat": 1.5, "optimism": 1.2,
    "optimistic": 1.2, "bullish": 2.0, "buy": 0.8, "milestone": 1.0, "innovation": 0.6,
    "resilient": 1.0, "resilience": 1.0, "improve": 1.0, "improves": 1.0, "improved": 1.0,
    "improvement": 1.0, "efficiency": 0.6, "invest": 0.5, "investment": 0.4, "listing": 0.3,
    "bonus": 1.0, "best": 1.0, "top": 0.2, "leads": 0.5, "lifts": 1.0, "lift": 1.0,
    "upside": 1.0, "positive": 1.0, "benefit": 0.8, "benefits": 0.8, "tops": 0.8,
    "doubles": 1.5, "triples": 1.5, "secures": 1.0, "secured": 1.0, "inflows": 0.8,
    # negative
    "loss": -1.5, "losses": -1.5, "lose": -1.2, "loses": -1.2, "lost": -1.0, "fall": -1.0,
    "falls": -1.0, "fell": -1.0, "falling": -1.0, "drop": -1.0, "drops": -1.0, "dropped": -1.0,
    "decline": -1.0, "declines": -1.0, "declined": -1.0, "plunge": -2.0, "plunges": -2.0,
    "plunged": -2.0, "slump": -2.0, "slumps": -2.0, "crash": -2.5, "crashes": -2.5,
    "tumble": -1.8, "tumbles": -1.8, "slide": -1.2, "slides": -1.2, "sink": -1.5, "sinks": -1.5,
    "down": -0.3, "lower": -0.7, "weak": -1.2, "weaker": -1.2, "weakens": -1.2, "slow": -0.8,
    "slows": -0.8, "slowdown": -1.2, "cut": -1.0, "cuts": -1.0, "slash": -1.5, "slashes": -1.5,
    "warning": -1.5, "warns": -1.5, "downgrade": -1.5, "downgrades": -1.5, "downgraded": -1.5,
    "debt": -0.6, "debts": -0.6, "default": -2.0, "defaults": -2.0, "lawsuit": -1.5, "sue": -1.2,
    "sues": -1.2, "sued": -1.2, "court": -0.6, "fraud": -2.5, "probe": -1.5, "investigation": -1.2,
    "scandal": -2.0, "fine": -1.0, "fined": -1.5, "penalty": -1.5, "suspend": -1.5,
    "suspends": -1.5, "suspended": -1.5, "suspension": -1.5, "delist": -2.0, "delisting": -2.0,
    "delisted": -2.0, "layoff": -1.5, "layoffs": -1.5, "redundancy": -1.2, "strike": -1.2,
    "halt": -1.5, "halts": -1.5, "halted": -1.5, "crisis": -2.0, "receivership": -2.5,
    "insolvency": -2.5, "insolvent": -2.5, "bankrupt": -3.0, "bankruptcy": -3.0,
    "collapse": -2.5, "collapses": -2.5, "freeze": -1.5, "freezes": -1.5, "frozen": -1.5,
    "dispute": -1.2, "disputes": -1.2, "row": -0.8, "fallout": -1.5, "risk": -0.5, "risks": -0.5,
    "concern": -0.8, "concerns": -0.8, "worries": -1.0, "fears": -1.2, "pressure": -0.8,
    "pressures": -0.8, "struggle": -1.2, "struggles": -1.2, "struggling": -1.2, "bearish": -2.0,
    "sell-off": -1.8, "selloff": -1.8, "dump": -1.2, "dumps": -1.2, "exit": -0.6, "exits": -0.6,
    "shutdown": -1.5, "closure": -1.0, "outage": -1.2, "negative": -1.0, "volatile": -0.6,
    "uncertainty": -1.0, "shortfall": -1.2, "deficit": -1.0, "arrears": -1.2, "unpaid": -1.0,
    "plummet": -2.0, "plummets": -2.0, "hit": -0.5, "hits": -0.3, "blow": -1.2, "woes": -1.5,
    "fails": -1.2, "failed": -1.2, "failure": -1.5, "worst": -1.5, "underperform": -1.5,
    "accused": -1.5, "corruption": -2.0, "ban": -1.2, "banned": -1.2, "losing": -1.0,
    "outflows": -0.8, "tax": -0.2, "levy": -0.5, "costs": -0.3, "headwinds": -1.2,
    "complaint": -0.8, "complaints": -0.8, "blackout": -1.0, "blackouts": -1.0, "rejects": -1.0,
    "rejected": -1.0, "missing": -0.5, "limbo": -1.2, "threat": -0.6, "threats": -0.6,
    "taxman": -0.5, "glitch": -1.0, "hitch": -0.8, "protest": -1.0, "protests": -1.0,
    "earnings": 0.2, "dividend-paying": 0.5, "owe": -1.0, "owes": -1.0, "owed": -0.8,
    "outages": -1.2, "rationing": -1.2, "scrutiny": -0.8, "fake": -0.6, "depletion": -0.6,
}

NEGATORS = {"not", "no", "never", "without", "fails", "failed", "unable", "didn't", "doesn't", "won't"}
INTENSIFIERS = {"sharply": 1.5, "significantly": 1.4, "massive": 1.5, "huge": 1.4, "steep": 1.4, "slightly": 0.5, "marginally": 0.5}

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_PHRASES_SORTED = sorted(PHRASES.items(), key=lambda kv: -len(kv[0]))

POS_THRESHOLD = 0.15
NEG_THRESHOLD = -0.15


def score_headline(text: str) -> tuple[float, str, list[str]]:
    low = " " + text.lower().replace("’", "'") + " "
    total = 0.0
    hits: list[str] = []
    # quantified moves are strong signals ("net earnings up 68.7%")
    for rx, w, tag in ((UP_MOVE, 1.2, "+x%"), (DOWN_MOVE, -1.2, "-x%")):
        if rx.search(low):
            total += w
            hits.append(tag)
    for phrase, w in _PHRASES_SORTED:
        pat = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
        if re.search(pat, low):
            total += w
            hits.append(phrase)
            low = re.sub(pat, " ", low)
    tokens = _TOKEN.findall(low)
    for i, tok in enumerate(tokens):
        w = WORDS.get(tok)
        if w is None:
            continue
        window = tokens[max(0, i - 3) : i]
        if any(t in NEGATORS for t in window):
            w = -0.8 * w
        for t in window:
            w *= INTENSIFIERS.get(t, 1.0)
        total += w
        hits.append(tok)
    # squash into [-1, 1]; more hits → more confidence
    score = math.tanh(total / 3.0)
    if score >= POS_THRESHOLD:
        label = "positive"
    elif score <= NEG_THRESHOLD:
        label = "negative"
    else:
        label = "neutral"
    return round(score, 4), label, hits[:12]
