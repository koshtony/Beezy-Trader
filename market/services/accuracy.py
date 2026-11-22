"""Forecast track record: past projections compared with the prices that actually followed.

Two sources, never mixed:
* live      the projections the app published (saved every day), checked once the target
            session has a final closing price;
* backtest  the price-history model replayed over past years (walk-forward), stored per stock.

Measures, for both:
* price accuracy   100% minus the average gap between forecast and actual price;
* direction right  share of up/down calls that were right, among prices that moved;
* inside range     share of actual prices inside the 90% range (well calibrated ≈ 90%);
* no-change guess  the same error for simply assuming today's price stays put. A forecast is only
                   useful if it beats that.
"""

import bisect
import datetime as dt
from collections import defaultdict

from django.db.models import Max
from django.utils import timezone

from ..models import JobLog, PriceBar, Projection

HORIZONS = ("1D", "1W", "1M", "1Y")
LABEL = {"1D": "1 day", "1W": "1 week", "1M": "1 month", "1Y": "1 year"}
ADJ = {"1D": "1-day", "1W": "1-week", "1M": "1-month", "1Y": "1-year"}
CLOSE_FINAL_AT = dt.time(16, 0)  # NSE closes 15:00; a price scrape after this has the official close
FLAT = 1e-9


def settled_through() -> dt.date | None:
    """Latest date whose closing prices are final in the database."""
    last = (
        JobLog.objects.filter(job="prices", status=JobLog.OK, finished_at__isnull=False)
        .values_list("finished_at", flat=True)
        .first()
    )
    if last is None:
        return None
    local = timezone.localtime(last)
    return local.date() if local.time() >= CLOSE_FINAL_AT else local.date() - dt.timedelta(days=1)


def _summary(recs: list[dict]) -> dict:
    n = len(recs)
    if not n:
        return {"n": 0}
    moved = [r for r in recs if r["direction"] != "flat"]
    right = sum(r["direction"] == "right" for r in moved)
    err = sum(r["error"] for r in recs) / n
    naive = sum(r["naive_error"] for r in recs) / n
    return {
        "n": n,
        "stocks": len({r["ticker"] for r in recs}),
        "batches": len({r["made"] for r in recs}),
        "first_made": min(r["made"] for r in recs),
        "last_made": max(r["made"] for r in recs),
        "error": err,
        "accuracy": 1 - err,
        "naive_error": naive,
        "skill": 1 - err / naive if naive > 0 else None,
        "moved": len(moved),
        "flat": n - len(moved),
        "right": right,
        "direction": right / len(moved) if moved else None,
        "inside": sum(r["in_range"] for r in recs) / n,
    }


def live() -> dict:
    """Every published projection whose target session has a final close, with its outcome."""
    through = settled_through()
    pending_q = Projection.objects.all() if through is None else Projection.objects.filter(target_date__gt=through)
    pending = {h: {"n": 0, "next": None} for h in HORIZONS}
    for h, n, nxt in _pending(pending_q):
        pending[h] = {"n": n, "next": nxt}
    if through is None:
        return {"through": None, "records": [], "by_horizon": {h: {"n": 0} for h in HORIZONS}, "pending": pending}

    projs = list(Projection.objects.filter(target_date__lte=through).select_related("stock"))
    if not projs:
        return {"through": through, "records": [], "by_horizon": {h: {"n": 0} for h in HORIZONS}, "pending": pending}

    start = min(p.as_of for p in projs)
    sessions = sorted(set(PriceBar.objects.filter(date__gte=start, date__lte=through).values_list("date", flat=True)))
    bars: dict[int, tuple[list, list]] = defaultdict(lambda: ([], []))
    for sid, d, c in (
        PriceBar.objects.filter(stock_id__in={p.stock_id for p in projs}, date__gte=start, date__lte=through)
        .order_by("stock_id", "date")
        .values_list("stock_id", "date", "close")
    ):
        bars[sid][0].append(d)
        bars[sid][1].append(c)

    records = []
    for p in projs:
        i = bisect.bisect_left(sessions, p.target_date)  # holidays: the next session that traded
        if i >= len(sessions):
            pending[p.horizon]["n"] += 1
            nxt = pending[p.horizon]["next"]
            pending[p.horizon]["next"] = p.target_date if nxt is None else min(nxt, p.target_date)
            continue
        session = sessions[i]
        dates, closes = bars.get(p.stock_id, ([], []))
        j = bisect.bisect_right(dates, session) - 1
        if j < 0 or not p.base_price or dates[j] < p.as_of:
            continue
        actual = closes[j]
        if not actual:
            continue
        predicted_move = p.expected_price - p.base_price
        actual_move = actual - p.base_price
        if abs(actual_move) <= FLAT * p.base_price:
            direction = "flat"
        else:
            direction = "right" if (predicted_move > 0) == (actual_move > 0) else "wrong"
        records.append({
            "ticker": p.stock.ticker,
            "name": p.stock.name,
            "horizon": p.horizon,
            "made": p.as_of,
            "target": session,
            "base": p.base_price,
            "forecast": p.expected_price,
            "low": p.low,
            "high": p.high,
            "actual": actual,
            "forecast_change": p.expected_price / p.base_price - 1,
            "actual_change": actual / p.base_price - 1,
            "error": abs(p.expected_price - actual) / actual,
            "naive_error": abs(p.base_price - actual) / actual,
            "direction": direction,
            "in_range": p.low <= actual <= p.high,
            "prob_up": p.prob_up,
        })
    records.sort(key=lambda r: (HORIZONS.index(r["horizon"]), r["made"], r["ticker"]))
    by_h = {h: _summary([r for r in records if r["horizon"] == h]) for h in HORIZONS}
    return {"through": through, "records": records, "by_horizon": by_h, "pending": pending}


def _pending(qs):
    from django.db.models import Count, Min

    for row in qs.values("horizon").annotate(n=Count("id"), nxt=Min("target_date")):
        yield row["horizon"], row["n"], row["nxt"]


def backtest() -> dict:
    """Aggregate the walk-forward backtests stored with each stock's latest projections."""
    latest = dict(Projection.objects.values_list("stock_id").annotate(m=Max("as_of")))
    acc = {h: defaultdict(float) for h in HORIZONS}
    for sid, h, as_of, bt in Projection.objects.filter(as_of__in=set(latest.values())).values_list(
        "stock_id", "horizon", "as_of", "backtest"
    ):
        if as_of != latest.get(sid) or not bt or not bt.get("samples") or h not in acc:
            continue
        a, n = acc[h], bt["samples"]
        moves = bt.get("moves", n)
        a["stocks"] += 1
        a["n"] += n
        a["moves"] += moves
        a["right"] += (bt.get("hit_rate") or 0) * moves
        a["inside"] += bt["band_coverage"] * n
        a["err"] += bt["mae"] * n
        a["naive"] += bt["mae_naive"] * n
        a["beats"] += int((bt.get("skill") or 0) > 0)
    out = {}
    for h, a in acc.items():
        if not a["n"]:
            out[h] = {"n": 0}
            continue
        err, naive = a["err"] / a["n"], a["naive"] / a["n"]
        out[h] = {
            "n": int(a["n"]),
            "stocks": int(a["stocks"]),
            "error": err,
            "accuracy": 1 - err,
            "naive_error": naive,
            "skill": 1 - err / naive if naive > 0 else None,
            "direction": a["right"] / a["moves"] if a["moves"] else None,
            "flat": 1 - a["moves"] / a["n"],
            "inside": a["inside"] / a["n"],
            "beats_share": a["beats"] / a["stocks"],
        }
    return out


def _p(x, nd=0):
    return f"{x * 100:.{nd}f}%"


def _signed(x, nd=0):
    return f"{x * 100:+.{nd}f}%".replace("-", "−")


def _d(d: dt.date) -> str:
    return f"{d.day} {d:%b}" if d.year == timezone.localdate().year else f"{d.day} {d:%b %Y}"


def notes(lv: dict, bt: dict) -> dict:
    """Plain-language reading of the track record (also shown under Labs → Notes)."""
    findings, meaning = [], []
    checked = [h for h in HORIZONS if lv["by_horizon"][h]["n"]]
    for h in checked:
        s = lv["by_horizon"][h]
        when = f"made {_d(s['first_made'])}" if s["batches"] == 1 else f"made {_d(s['first_made'])} to {_d(s['last_made'])}"
        line = (f"{ADJ[h].capitalize()} forecasts ({s['n']} checked, {when}): prices were off by {_p(s['error'], 1)} on average, "
                f"so {_p(s['accuracy'], 1)} accurate. {_p(s['inside'])} landed inside the 90% range.")
        if s["moved"]:
            line += f" Direction was right for {s['right']} of the {s['moved']} stocks that moved ({_p(s['direction'])})."
        findings.append(line)
        if s["skill"] is not None:
            better = s["error"] < s["naive_error"]
            findings.append(f"Simply assuming no change would have been off by {_p(s['naive_error'], 1)}, so the model was "
                            + ("closer" if better else "no closer") + f" ({_signed(s['skill'])} skill).")
    if not checked:
        findings.append("No published forecast has reached its target date yet.")
    waiting = [h for h in HORIZONS if lv["pending"][h]["n"] and lv["pending"][h]["next"]]
    if waiting:
        findings.append("Still to be checked: " + "; ".join(
            f"{lv['pending'][h]['n']} {ADJ[h]} forecasts (next on {_d(lv['pending'][h]['next'])})" for h in waiting) + ".")

    bt_lines = []
    for h in HORIZONS:
        b = bt.get(h, {})
        if b.get("n") and b.get("direction") is not None:
            bt_lines.append(f"{ADJ[h]}: direction {_p(b['direction'])} right, {_p(b['inside'])} inside the range, "
                            f"beat a no-change guess for {_p(b['beats_share'])} of stocks")
    if bt_lines:
        findings.append("Backtest over past years: " + "; ".join(bt_lines) + ".")

    total_live = sum(lv["by_horizon"][h]["n"] for h in HORIZONS)
    batches = max((lv["by_horizon"][h].get("batches", 0) for h in HORIZONS), default=0)
    if total_live and batches < 10:
        meaning.append(f"{total_live} checks from {batches} day{'s' if batches != 1 else ''} of forecasts is too few to "
                       "judge. One market-wide move can swing every result at once; the figures settle after a few weeks.")
    if "1D" in checked:
        meaning.append("A 1-day price accuracy near 99% says little on its own: prices rarely move far in a day. "
                       "Compare it with the no-change guess.")
    inside = [bt[h]["inside"] for h in HORIZONS if bt.get(h, {}).get("n")]
    if inside:
        avg_in = sum(inside) / len(inside)
        if avg_in >= 0.85:
            meaning.append(f"The 90% ranges have held about {_p(avg_in)} of the time in the backtest, so the range is the "
                           "most dependable part of each projection.")
        else:
            meaning.append(f"The 90% ranges held only {_p(avg_in)} of the time in the backtest, so expect wider swings than shown.")
    dirs = [bt[h]["direction"] for h in HORIZONS if bt.get(h, {}).get("direction") is not None]
    if dirs and max(dirs) < 0.6:
        meaning.append(f"Direction calls are close to a coin flip ({_p(min(dirs))} to {_p(max(dirs))} right), and the "
                       "expected price is about as close as assuming no change. Use projections for the likely range and "
                       "for comparing stocks, not as a buy-or-sell timing signal.")
    bottom = None
    if inside and dirs:
        if sum(inside) / len(inside) >= 0.85 and max(dirs) < 0.6:
            bottom = "Trust the projected ranges more than the expected prices; past direction calls were near a coin flip."
        elif max(dirs) >= 0.6:
            bottom = "Direction calls have held up better than chance, but always check the range too."
    return {
        "key": "accuracy",
        "title": "Forecast accuracy",
        "bottom": bottom,
        "measures": "Each day the app saves its forecasts. Once the target day's closing price is in, each forecast is "
        "compared with what actually happened. The backtest replays the model over past years for a longer record.",
        "findings": findings,
        "meaning": meaning,
        "caveats": [
            "The backtest replays only the price-history part of the model; published forecasts also use earnings, "
            "valuation, news and analyst targets.",
            "Prices that didn't move (common on thinly traded counters) are left out of the direction score.",
        ],
    }


def report() -> dict:
    lv, bt = live(), backtest()
    latest = {}
    for h in HORIZONS:
        recs = [r for r in lv["records"] if r["horizon"] == h]
        if recs:
            last = max(r["made"] for r in recs)
            latest[h] = [r for r in recs if r["made"] == last]
    rows = sorted((r for rs in latest.values() for r in rs), key=lambda r: (HORIZONS.index(r["horizon"]), r["ticker"]))
    head = next((h for h in HORIZONS if lv["by_horizon"][h]["n"]), None)
    return {
        "through": lv["through"].isoformat() if lv["through"] else None,
        "horizons": [{"key": h, "label": LABEL[h], "adj": ADJ[h], "live": lv["by_horizon"][h], "pending": lv["pending"][h],
                      "backtest": bt.get(h, {"n": 0})} for h in HORIZONS],
        "headline": {"key": head, "adj": ADJ[head], **lv["by_horizon"][head]} if head else None,
        "next_check": min((lv["pending"][h]["next"] for h in HORIZONS if lv["pending"][h]["next"]), default=None),
        "latest": rows,
        "chart": {
            "labels": ADJ,
            "scatter": {
                h: [{"t": r["ticker"], "f": r["forecast_change"], "a": r["actual_change"], "in": r["in_range"]} for r in rs]
                for h, rs in latest.items()
            },
        },
        "notes": notes(lv, bt),
    }
