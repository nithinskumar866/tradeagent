"""Small formatting helpers shared by reports, CLI and dashboard."""

from __future__ import annotations

import math


def inr(x: float | None, decimals: int = 2) -> str:
    """Format a rupee amount with Indian digit grouping: 123456.5 → '₹1,23,456.50'."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    neg = x < 0
    s = f"{abs(x):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = f"₹{whole}" + (f".{frac}" if frac else "")
    return f"-{out}" if neg else out


def pct(x: float | None, decimals: int = 1, signed: bool = False) -> str:
    """Format a percentage value (already in percent units)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"{x:+.{decimals}f}%" if signed else f"{x:.{decimals}f}%"
