"""Macro and company (micro) factors that move NSE shares.

* environment   is each macro factor rising, falling or steady right now (from the CBK/EIA data)?
* exposures     who that helps or hurts, and why (sector rules, written down so they can be checked)
* sensitivity   did it actually show up in prices? Monthly returns over 5 years regressed on the
                market, T-bill moves, the shilling and oil
* events        company news tagged by type (results, dividends, regulation, deals...)

Shown alongside the scores; the scores themselves are unchanged.
"""

import bisect
import datetime as dt
import math
import re
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from django.utils import timezone

from ..models import MacroPoint, NewsArticle, PriceBar, Stock
from ..security import safe_url
from . import catalog
from .macro import SERIES

# ---------------------------------------------------------------- macro environment


@dataclass(frozen=True)
class Factor:
    key: str
    name: str
    series: str
    months: int  # look-back for the trend
    threshold: float  # change that counts as a move
    relative: bool  # % change (prices) or point change (rates)
    rising: str  # plain-language name of a rise
    falling: str
    unit_note: str


FACTORS = [
    Factor("rates", "Interest rates", "tbill91", 6, 0.5, False, "Rates rising", "Rates falling",
           "91-day T-bill, change over 6 months"),
    Factor("inflation", "Inflation", "inflation", 3, 0.5, False, "Inflation rising", "Inflation easing",
           "12-month CPI inflation, change over 3 months"),
    Factor("shilling", "Shilling vs dollar", "usdkes", 3, 0.015, True, "Shilling weakening", "Shilling strengthening",
           "USD/KES, change over 3 months"),
    Factor("oil", "Oil price", "brent", 3, 0.10, True, "Oil rising", "Oil falling", "Brent crude, change over 3 months"),
]
FACTOR = {f.key: f for f in FACTORS}
IF_UP = {"rates": "If rates rise", "inflation": "If inflation rises", "shilling": "If the shilling weakens",
         "oil": "If oil rises"}

# CBK's medium-term inflation target: 5% ± 2.5 points.
INFLATION_TARGET = (2.5, 7.5)


@dataclass(frozen=True)
class Exposure:
    factor: str
    targets: tuple[str, ...]  # sector names or tickers
    up: int  # +1 helped when the factor rises, -1 hurt, 0 mixed
    why_up: str
    why_down: str


EXPOSURES = [
    # interest rates
    Exposure("rates", ("Banking",), +1,
             "Higher yields on government securities and new loans widen margins (watch for more bad loans).",
             "Loans reprice lower under the CBR and KESONIA, squeezing margins; cheaper credit can lift lending and cut bad loans."),
    Exposure("rates", ("Insurance",), 0,
             "Higher bond yields lift investment income, but falling bond prices cut this year's valuations.",
             "Bond price gains boost profits now, but reinvesting at lower yields earns less later."),
    Exposure("rates", ("KPLC", "KEGN", "KQ", "TCL"), -1,
             "Heavily borrowed: higher rates raise interest costs.",
             "Heavily borrowed: lower rates ease interest costs."),
    Exposure("rates", ("Real Estate Investment Trust", "HAFR", "CTUM"), -1,
             "Rental and property yields look less attractive next to higher bond yields.",
             "Property yields look more attractive as bond yields fall."),
    Exposure("rates", ("EABL", "BAT", "UNGA", "CRWN", "QMRT", "CGEN"), -1,
             "Dearer credit slows consumer spending and raises working-capital costs.",
             "Cheaper credit supports consumer spending."),
    Exposure("rates", ("NSE",), -1,
             "Investors move money into T-bills, so share trading volumes fall.",
             "Money returns to shares as T-bills pay less, lifting trading volumes."),
    # inflation
    Exposure("inflation", ("EABL", "BAT", "UNGA", "CRWN", "BOC", "CARB", "QMRT"), -1,
             "Input costs rise and households cut back on spending.",
             "Costs settle and household budgets stretch further."),
    Exposure("inflation", ("NMG", "SGL", "SCAN", "TPSE"), -1,
             "Advertising, travel and leisure budgets are the first to be cut.",
             "Discretionary spending on ads, travel and leisure recovers."),
    Exposure("inflation", ("Banking",), +1,
             "Usually prompts CBK to hold or raise rates, which supports bank margins.",
             "Gives CBK room to cut rates, which narrows bank margins."),
    # shilling (USD/KES up = shilling weaker)
    Exposure("shilling", ("WTK", "KAPC", "LIMT", "KUKZ", "SASN", "EGAD"), +1,
             "Exporters paid in dollars (tea, coffee, avocados, macadamia) earn more shillings.",
             "Dollar export earnings are worth fewer shillings."),
    Exposure("shilling", ("KQ", "KPLC", "KEGN", "TOTL"), -1,
             "Dollar loans, fuel and power purchases cost more in shillings.",
             "Dollar loans, fuel and power purchases get cheaper in shillings."),
    Exposure("shilling", ("EABL", "CRWN", "UNGA", "BOC", "CARB", "CGEN"), -1,
             "Imported raw materials and goods cost more.",
             "Imported raw materials and goods get cheaper."),
    Exposure("shilling", ("SCOM", "EQTY", "KCB", "COOP", "ABSA", "SCBK"), -1,
             "Foreign investors' returns shrink in dollars, so they tend to sell the big names.",
             "Dollar returns improve, drawing foreign buyers back to the big names."),
    # oil
    Exposure("oil", ("KQ",), -1, "Jet fuel is one of the airline's biggest costs.", "Jet fuel, a major cost, gets cheaper."),
    Exposure("oil", ("KPLC",), -1,
             "Thermal power costs rise and reach bills with a lag, which hurts demand.",
             "Lower thermal power costs ease bills and support demand."),
    Exposure("oil", ("TOTL",), 0,
             "Stock on hand gains value, but fuel needs more working capital and margins are set by EPRA.",
             "Stock on hand loses value, though working-capital needs fall."),
    Exposure("oil", ("EABL", "BAT", "CRWN", "UNGA", "BOC", "XPRS", "CGEN"), -1,
             "Transport, packaging and energy costs rise.",
             "Transport, packaging and energy costs fall."),
]

SHORT_NAMES = {"Real Estate Investment Trust": "REITs", "Manufacturing & Allied": "Manufacturers",
               "Commercial & Services": "Commercial & services"}

MARKET_NOTES = {
    "rates": ("Higher T-bill yields compete with shares for investors' money.",
              "Lower T-bill yields push investors toward shares and dividends."),
    "inflation": ("High inflation erodes real returns and usually keeps rates up.",
                  "Easing inflation lets CBK cut rates, which usually helps shares."),
    "shilling": ("A weaker shilling makes Kenyan assets less attractive to foreign investors.",
                 "A stable or stronger shilling supports foreign buying."),
    "oil": ("Dearer oil feeds through to fuel prices and inflation.", "Cheaper oil eases fuel prices and inflation."),
}


def _expand(targets: tuple[str, ...]) -> list[str]:
    out = []
    for t in targets:
        out.extend(catalog.SECTORS.get(t, [t]))
    return [t for t in out if t not in catalog.NON_EQUITY]


def rules_for(ticker: str) -> list[Exposure]:
    return [e for e in EXPOSURES if ticker in _expand(e.targets)]


def _points(series: str) -> tuple[list[dt.date], list[float]]:
    rows = list(MacroPoint.objects.filter(series=series).order_by("date").values_list("date", "value"))
    return [d for d, _ in rows], [v for _, v in rows]


def _at(dates, values, when: dt.date):
    i = bisect.bisect_right(dates, when) - 1
    return (dates[i], values[i]) if i >= 0 else (None, None)


def _months_back(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    import calendar

    return dt.date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def trend(f: Factor) -> dict:
    dates, values = _points(f.series)
    if not dates:
        return {"key": f.key, "name": f.name, "direction": "unknown", "available": False}
    last_d, last_v = dates[-1], values[-1]
    then_d, then_v = _at(dates, values, _months_back(last_d, f.months))
    if then_v is None:
        return {"key": f.key, "name": f.name, "direction": "unknown", "available": False}
    change = (last_v / then_v - 1) if f.relative else (last_v - then_v)
    direction = "rising" if change > f.threshold else "falling" if change < -f.threshold else "steady"
    return {
        "key": f.key, "name": f.name, "series": f.series, "available": True,
        "value": last_v, "date": last_d, "then": then_v, "then_date": then_d,
        "change": change, "relative": f.relative, "direction": direction,
        "label": f.rising if direction == "rising" else f.falling if direction == "falling" else f"{f.name} steady",
        "measure": f.unit_note,
    }


def policy_rate() -> dict:
    """Latest CBR decision and the one before it."""
    dates, values = _points("cbr")
    if not dates:
        return {}
    last = {"rate": values[-1], "date": dates[-1]}
    for d, v in zip(reversed(dates[:-1]), reversed(values[:-1])):
        if v != values[-1]:
            last.update(prev=v, changed_on=next(dd for dd, vv in zip(dates, values) if dd > d and vv == values[-1]))
            break
    return last


def environment() -> list[dict]:
    out = []
    for f in FACTORS:
        t = trend(f)
        helps, hurts, mixed, watch = [], [], [], []
        if t["available"]:
            for e in EXPOSURES:
                if e.factor != f.key:
                    continue
                entry = {"targets": list(e.targets), "tickers": _expand(e.targets),
                         "who": ", ".join(SHORT_NAMES.get(x, x) for x in e.targets),
                         "why": e.why_down if t["direction"] == "falling" else e.why_up}
                if t["direction"] not in ("rising", "falling"):  # steady: just say who'd feel a move
                    watch.append(entry)
                    continue
                effect = e.up * (1 if t["direction"] == "rising" else -1)
                (helps if effect > 0 else hurts if effect < 0 else mixed).append(entry)
            note = MARKET_NOTES[f.key]
            t["market_note"] = note[0] if t["direction"] == "rising" else note[1] if t["direction"] == "falling" else ""
        if f.key == "inflation" and t["available"]:
            lo, hi = INFLATION_TARGET
            t["band"] = ("above CBK's 2.5–7.5% target band" if t["value"] > hi else
                         "below CBK's target band" if t["value"] < lo else
                         "near the top of CBK's 2.5–7.5% target band" if t["value"] > hi - 1 else "inside CBK's 2.5–7.5% target band")
        t.update(helps=helps, hurts=hurts, mixed=mixed, watch=watch)
        out.append(t)
    return out


# ---------------------------------------------------------------- measured sensitivity
SENS_MONTHS = 60
MIN_MONTHS = 36


def _month_key(d: dt.date) -> tuple[int, int]:
    return d.year, d.month


def _monthly_last(rows) -> dict[tuple[int, int], float]:
    out = {}
    for d, v in rows:
        out[_month_key(d)] = v
    return out


def sensitivity() -> dict:
    """Per stock: monthly log return ~ market + ΔT-bill (pp) + ΔlogUSD/KES + ΔlogBrent, last 5 years."""
    macro = {s: _monthly_last(MacroPoint.objects.filter(series=s).order_by("date").values_list("date", "value"))
             for s in ("tbill91", "usdkes", "brent")}
    closes: dict[int, dict] = defaultdict(dict)
    for sid, d, c in PriceBar.objects.order_by("stock_id", "date").values_list("stock_id", "date", "close"):
        closes[sid][_month_key(d)] = c
    index = Stock.objects.filter(ticker="NASI").first()
    if index is None or not all(macro.values()) or index.id not in closes:
        return {"rows": [], "months": 0}
    today = timezone.localdate()
    current = _month_key(today)
    months = sorted(k for k in closes[index.id] if k < current)  # complete months only
    months = [m for m in months if all(m in macro[s] for s in macro)][-(SENS_MONTHS + 1):]
    if len(months) < MIN_MONTHS + 1:
        return {"rows": [], "months": 0}
    nasi = closes[index.id]

    def diffs(get, log=True):
        vals = [get(m) for m in months]
        return np.array([
            (math.log(b / a) if log else b - a) if a and b else np.nan for a, b in zip(vals[:-1], vals[1:])
        ])

    x_mkt = diffs(lambda m: nasi.get(m))
    x_rate = diffs(lambda m: macro["tbill91"][m], log=False)
    x_fx = diffs(lambda m: macro["usdkes"][m])
    x_oil = diffs(lambda m: macro["brent"][m])
    rows = []
    for st in Stock.objects.filter(kind=Stock.KIND_EQUITY).exclude(ticker__in=catalog.NON_EQUITY).order_by("ticker"):
        series = closes.get(st.id)
        if not series:
            continue
        y = diffs(lambda m: series.get(m))
        ok = ~np.isnan(y) & ~np.isnan(x_mkt)
        if ok.sum() < MIN_MONTHS or (y[ok] != 0).mean() < 0.7:  # too short, or too thinly traded to say
            continue
        X = np.column_stack([np.ones(ok.sum()), x_mkt[ok], x_rate[ok], x_fx[ok], x_oil[ok]])
        coef, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        resid = y[ok] - X @ coef
        dof = len(resid) - X.shape[1]
        if dof <= 5:
            continue
        cov = (resid @ resid / dof) * np.linalg.pinv(X.T @ X)
        se = np.sqrt(np.clip(np.diag(cov), 1e-18, None))
        t = coef / se
        rows.append({
            "ticker": st.ticker, "name": st.name, "sector": st.sector, "months": int(ok.sum()),
            "market": float(coef[1]),
            # effects scaled to a typical move: +1 pp T-bill, +5% USD/KES, +20% Brent (in % of share price)
            "rates": float(coef[2] * 100), "rates_t": float(t[2]),
            "shilling": float(coef[3] * 0.05 * 100), "shilling_t": float(t[3]),
            "oil": float(coef[4] * 0.20 * 100), "oil_t": float(t[4]),
        })
    for r in rows:
        for e in rules_for(r["ticker"]):
            if e.up and e.factor in ("rates", "shilling", "oil"):
                b, tv = r[e.factor], r[f"{e.factor}_t"]
                r[f"{e.factor}_rule"] = e.up
                r[f"{e.factor}_check"] = "confirmed" if abs(tv) >= 2 and math.copysign(1, b) == e.up else (
                    "contradicted" if abs(tv) >= 2 else "unclear")
    return {"rows": rows, "months": len(months) - 1, "start": f"{months[0][0]}-{months[0][1]:02d}",
            "end": f"{months[-1][0]}-{months[-1][1]:02d}"}


# ---------------------------------------------------------------- company events (micro)
EVENT_TYPES = [
    ("warning", "Profit warning", r"profit warning"),
    ("results", "Results", r"\b(profits?|earnings|results|net income|revenue|turnover|half[- ]year|full[- ]year|"
                           r"nine[- ]month|pre-?tax|after tax|losses?|h1|q[1-4])\b"),
    ("dividend", "Dividend", r"\b(dividends?|payout|bonus shares?|book closure)\b"),
    ("capital", "Capital raise", r"\b(rights issue|share sale|capital raise|raise capital|bond issue|green bond|"
                                 r"private placement|core capital|capital injection)\b"),
    ("people", "Leadership", r"\b(ceo|chief executive|managing director|chairman|chairperson|board|appoint\w*|"
                             r"resign\w*|steps down|names new)\b"),
    ("regulation", "Regulation & tax", r"\b(cbk|central bank|cma|capital markets authority|epra|communications authority|"
                                       r"kra|(?<!after )(?<!pre-)(?<!before )tax(es)?|excise|levy|regulat\w*|licen[cs]e\w*|"
                                       r"fined|fines?|penalt\w*|"
                                       r"finance bill|finance act|tariffs?)\b"),
    ("deal", "Deals", r"\b(acqui\w+|mergers?|merge|takeover|stake|buyout|joint venture|divest\w*|partnership)\b"),
    ("legal", "Legal", r"\b(court|lawsuit|sued|sues|tribunal|petition|ruling|arbitration)\b"),
    ("rating", "Credit rating", r"\b(moody'?s|fitch|s&p|credit rating|downgraded?|upgraded?)\b"),
]
_EVENT_RE = [(k, label, re.compile(rx, re.I)) for k, label, rx in EVENT_TYPES]
EVENT_LABEL = {k: label for k, label, _ in EVENT_TYPES}


def tag(title: str) -> list[str]:
    tags = [k for k, _, rx in _EVENT_RE if rx.search(title)]
    if "warning" in tags and "results" in tags:
        tags.remove("results")
    return tags


def events(days: int = 30, ticker: str | None = None, limit: int = 200) -> list[dict]:
    since = timezone.now() - dt.timedelta(days=days)
    qs = NewsArticle.objects.filter(published_at__gte=since, stock__kind=Stock.KIND_EQUITY).select_related("stock")
    if ticker:
        qs = qs.filter(stock__ticker=ticker)
    out, seen = [], {}
    for a in qs.order_by("-published_at")[:2000]:
        tags = tag(a.title)
        if not tags:
            continue
        # several outlets carrying the same story: one line per stock, day and event type
        key = (a.stock_id, timezone.localtime(a.published_at).date(), tuple(tags))
        if key in seen:
            seen[key]["similar"] += 1
            continue
        if len(out) >= limit:
            continue
        item = {
            "ticker": a.stock.ticker, "name": a.stock.name, "title": a.title, "url": safe_url(a.url),
            "source": a.source, "published_at": a.published_at, "label": a.label, "tags": tags,
            "tag_labels": [EVENT_LABEL[t] for t in tags], "similar": 0,
        }
        seen[key] = item
        out.append(item)
    return out


SECTOR_DRIVERS = {
    "Banking": [
        "Bad-loan (NPL) ratio and provisions",
        "Loan pricing under the CBR and KESONIA",
        "Minimum core capital rising to KES 10bn by 2029 (rights issues, mergers)",
        "Yields on government securities",
        "Regional subsidiaries and their currencies",
    ],
    "Insurance": ["Government bond yields and valuation gains", "Motor and medical claims", "IRA capital rules"],
    "Telecommunication": [
        "M-Pesa growth and fees", "Ethiopia losses and the birr", "Excise duty on airtime, data and mobile money",
        "Communications Authority and CBK rules",
    ],
    "Manufacturing & Allied": [
        "Excise duty in each year's Finance Act (beer, spirits, cigarettes)", "Imported raw materials and the shilling",
        "Illicit trade", "Household spending",
    ],
    "Energy & Petroleum": [
        "EPRA tariff and pump-price reviews", "Rainfall (hydro) and geothermal output", "Fuel costs and the shilling",
        "Government arrears and subsidies",
    ],
    "Agricultural": ["Mombasa tea auction prices", "Weather and harvests", "Shilling (export earnings)",
                     "Avocado and macadamia export prices"],
    "Commercial & Services": ["Household spending", "Advertising budgets (media)", "Tourist arrivals (hotels)",
                              "Fuel prices (airline, logistics)"],
    "Construction & Allied": ["Government infrastructure spending", "Cement demand and energy costs"],
    "Investment": ["Portfolio and property valuations", "Interest rates vs property yields"],
    "Real Estate Investment Trust": ["Occupancy and rents", "Interest rates vs rental yields"],
    "Investment Services": ["Market turnover and new listings", "T-bill yields competing for investors"],
    "Automobiles & Accessories": ["Vehicle and machinery demand", "Imported costs and the shilling"],
}


# ---------------------------------------------------------------- views
def tiles() -> list[dict]:
    out = []
    pol = policy_rate()
    if pol:
        decided, _ = _points("cbr")
        last_mpc = decided[-1]
        if pol.get("prev") is not None:
            moved = f"{'cut' if pol['rate'] < pol['prev'] else 'raised'} from {pol['prev']:g}% in {pol['changed_on']:%b %Y}"
            sub = moved if pol["changed_on"] == last_mpc else f"held {last_mpc:%-d %b %Y} · {moved}"
        else:
            sub = f"set {last_mpc:%-d %b %Y}"
        out.append({"key": "cbr", "name": "Central Bank Rate", "value": pol["rate"], "unit": "%", "date": last_mpc, "sub": sub})
    for key, months in (("tbill91", 6), ("inflation", 3), ("usdkes", 3), ("brent", 3), ("lending", 6)):
        dates, values = _points(key)
        if not dates:
            continue
        name, unit, _ = SERIES[key]
        _, then = _at(dates, values, _months_back(dates[-1], months))
        rel = unit not in ("%",)
        change = None if then is None else (values[-1] / then - 1 if rel else values[-1] - then)
        out.append({"key": key, "name": name, "value": values[-1], "unit": unit, "date": dates[-1], "change": change,
                    "relative": rel, "months": months})
    kes = _points("kesonia")
    if kes[0]:
        out.append({"key": "kesonia", "name": "KESONIA", "value": kes[1][-1], "unit": "%", "date": kes[0][-1],
                    "sub": f"loan-pricing benchmark · {kes[0][-1]:%-d %b %Y}"})
    return out


def charts(years: int = 5) -> dict:
    since = timezone.localdate() - dt.timedelta(days=365 * years)
    out = {}
    for key in ("cbr", "tbill91", "inflation", "lending", "usdkes", "brent"):
        rows = list(MacroPoint.objects.filter(series=key, date__gte=since).order_by("date").values_list("date", "value"))
        if key == "cbr":  # carry the rate in force at the start of the window
            before = MacroPoint.objects.filter(series=key, date__lt=since).order_by("-date").values_list("value", flat=True).first()
            if before is not None:
                rows.insert(0, (since, before))
        if key == "brent":  # weekly is plenty for a 5-year chart
            rows = rows[::5] + rows[-1:]
        out[key] = {"dates": [d.isoformat() for d, _ in rows], "values": [v for _, v in rows]}
    return out


def overview() -> dict:
    env = environment()
    sens = sensitivity()
    ev = events(30)
    counts = defaultdict(int)
    for e in ev:
        for t in e["tags"]:
            counts[t] += 1
    return {
        "tiles": tiles(),
        "environment": env,
        "sensitivity": sens,
        "events": ev,
        "event_counts": [{"key": k, "label": EVENT_LABEL[k], "n": counts[k]} for k, _, _ in EVENT_TYPES if counts[k]],
        "drivers": SECTOR_DRIVERS,
        "charts": charts(),
        "updated": MacroPoint.objects.order_by("-date").values_list("date", flat=True).first(),
    }


def for_stock(ticker: str, sector: str, sens_rows: list[dict] | None = None) -> dict:
    env = {e["key"]: e for e in environment()}
    measured = next((r for r in (sens_rows or []) if r["ticker"] == ticker), None)
    items = []
    for e in rules_for(ticker):
        t = env.get(e.factor, {})
        direction = t.get("direction", "unknown")
        if direction in ("rising", "falling"):
            effect = e.up * (1 if direction == "rising" else -1)
            status = "tailwind" if effect > 0 else "headwind" if effect < 0 else "mixed"
            why = e.why_up if direction == "rising" else e.why_down
        else:
            status, why = "watch", f"{IF_UP[e.factor]}: {e.why_up[0].lower()}{e.why_up[1:]}"
        item = {"factor": e.factor, "name": FACTOR[e.factor].name, "label": t.get("label", ""), "status": status,
                "why": why, "direction": direction}
        if measured and e.factor in ("rates", "shilling", "oil"):
            item["measured"] = measured[e.factor]
            item["measured_t"] = measured[f"{e.factor}_t"]
            item["check"] = measured.get(f"{e.factor}_check")
        items.append(item)
    order = {"headwind": 0, "tailwind": 1, "mixed": 2, "watch": 3}
    items.sort(key=lambda i: order[i["status"]])
    return {"exposures": items, "events": events(60, ticker, limit=8), "drivers": SECTOR_DRIVERS.get(sector, []),
            "measured": measured}
