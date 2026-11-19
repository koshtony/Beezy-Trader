"""Formatting filters for the ledger UI."""

import math

from django import template
from django.utils.dateparse import parse_datetime

register = template.Library()


def _ok(x):
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


@register.filter
def num(x, nd=2):
    if not _ok(x):
        return "—"
    return f"{float(x):,.{int(nd)}f}"


@register.filter
def signed(x, nd=2):
    if not _ok(x):
        return "—"
    return f"{float(x):+,.{int(nd)}f}"


@register.filter
def pct(x, nd=1):
    """Fraction → signed percent (0.052 → +5.2%)."""
    if not _ok(x):
        return "—"
    return f"{float(x) * 100:+.{int(nd)}f}%"


@register.filter
def pctu(x, nd=0):
    """Fraction → unsigned percent (0.71 → 71%)."""
    if not _ok(x):
        return "—"
    return f"{float(x) * 100:.{int(nd)}f}%"


@register.filter
def pctv(x, nd=1):
    """Value already in percent units (41.97 → 41.97%)."""
    if not _ok(x):
        return "—"
    return f"{float(x):.{int(nd)}f}%"


@register.filter
def spctv(x, nd=2):
    """Signed value already in percent units."""
    if not _ok(x):
        return "—"
    return f"{float(x):+.{int(nd)}f}%"


@register.filter
def big(x):
    if not _ok(x):
        return "—"
    x = float(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:,.2f}{suf}"
    return f"{x:,.0f}"


@register.filter
def tone(x):
    """CSS class for gains/losses."""
    if not _ok(x):
        return "flat"
    x = float(x)
    return "up" if x > 0 else "down" if x < 0 else "flat"


@register.filter
def arrow(x):
    if not _ok(x):
        return ""
    return "▲" if float(x) > 0 else "▼" if float(x) < 0 else "■"


@register.filter
def metric(x, unit):
    if unit == "x":
        return "—" if not _ok(x) else f"{float(x):.2f}×"
    if unit == "pct":
        return pctv(x)
    if unit == "kes":
        return "—" if not _ok(x) else f"KES {float(x):,.2f}"
    if unit == "big":
        return "—" if not _ok(x) else f"KES {big(x)}"
    return num(x)


@register.filter
def get(d, key):
    if isinstance(d, dict):
        return d.get(key)
    return None


@register.filter
def score_band(x):
    if not _ok(x):
        return "na"
    x = float(x)
    return "good" if x >= 65 else "fair" if x >= 45 else "poor"


@register.filter
def risk_band(x):
    if not _ok(x):
        return "na"
    x = float(x)
    return "low" if x < 35 else "mid" if x < 60 else "high"


@register.filter
def iso(s):
    return parse_datetime(s) if isinstance(s, str) else s


@register.filter
def width(x):
    """0..100 score → CSS width percentage, clamped."""
    if not _ok(x):
        return 0
    return max(2, min(100, round(float(x))))
