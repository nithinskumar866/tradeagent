"""Position sizing and portfolio limits.

    risk_amount = capital × risk_per_trade_pct/100 × regime multiplier
    qty = floor(risk_amount / (entry − stop))
    qty = min(qty, floor(capital × max_position_pct/100 / entry))
    qty = min(qty, floor(available_cash / entry))
    qty < 1 → reject ("position too small for this capital")
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .costs import round_trip_costs
from .scanner import Signal

TOO_SMALL = "position too small for this capital"


@dataclass
class SizeResult:
    """Outcome of sizing one trade."""

    qty: int
    risk_amount: float          # qty × (entry − stop): the rupees actually at risk
    risk_budget: float          # what the rules allowed
    capped_by: str | None       # None | "max_position_pct" | "cash"
    reason: str | None          # rejection reason when qty < 1

    @property
    def ok(self) -> bool:
        """True if at least one share can be bought."""
        return self.qty >= 1


def position_size(
    entry: float,
    stop: float,
    capital: float,
    available_cash: float,
    cfg: Config,
    risk_multiplier: float = 1.0,
) -> SizeResult:
    """Shares to buy so that a stop-out loses about ``risk_per_trade_pct`` of capital."""
    a = cfg.account
    budget = capital * a.risk_per_trade_pct / 100 * risk_multiplier
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return SizeResult(0, 0.0, budget, None, "invalid entry/stop")
    qty = math.floor(budget / per_share)
    capped = None
    max_pos = math.floor(capital * a.max_position_pct / 100 / entry)
    if max_pos < qty:
        qty, capped = max_pos, "max_position_pct"
    by_cash = math.floor(max(available_cash, 0) / entry)
    if by_cash < qty:
        qty, capped = by_cash, "cash"
    if qty < 1:
        return SizeResult(0, 0.0, budget, capped, TOO_SMALL)
    return SizeResult(qty, qty * per_share, budget, capped, None)


def sizing_steps(entry: float, stop: float, capital: float, available_cash: float, cfg: Config,
                 risk_multiplier: float = 1.0, regime: str | None = None) -> list[str]:
    """The quantity worked out step by step in plain words (same maths as ``position_size``)."""
    from .fmt import inr

    a = cfg.account
    size = position_size(entry, stop, capital, available_cash, cfg, risk_multiplier)
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        return ["No valid stop below the entry, so no quantity."]
    mult = f" × {risk_multiplier:g} ({regime} market)" if risk_multiplier != 1 else ""
    budget = size.risk_budget
    by_risk = math.floor(budget / per_share)
    pos_cap = capital * a.max_position_pct / 100
    by_pos = math.floor(pos_cap / entry)
    by_cash = math.floor(max(available_cash, 0) / entry)
    all_in = math.floor(capital / entry)
    lines = [
        f"1. Risk limit: {inr(capital, 0)} × {a.risk_per_trade_pct:g}%{mult} = {inr(budget)} you may lose if the stop "
        f"is hit. Each share loses {inr(entry)} − {inr(stop)} = {inr(per_share)} → {inr(budget)} ÷ {inr(per_share)} "
        f"= **{by_risk} shares**.",
        f"2. One-stock limit: {a.max_position_pct:g}% of {inr(capital, 0)} = {inr(pos_cap, 0)} → "
        f"{inr(pos_cap, 0)} ÷ {inr(entry)} = **{by_pos} shares**.",
        f"3. Cash: {inr(available_cash, 0)} free → **{by_cash} shares**.",
        f"Quantity = the smallest of the three = **{size.qty}**"
        + (f" ({size.reason})." if size.reason else
           f", costing {inr(size.qty * entry, 0)}, with {inr(size.risk_amount)} at risk."),
        f"Spending all {inr(capital, 0)} would buy {all_in} shares, but a stop-out would then lose "
        f"{inr(all_in * per_share)} ({all_in * per_share / capital * 100:.1f}% of capital) in one trade; "
        f"the rules cap a loss at {a.risk_per_trade_pct:g}%{' × ' + format(risk_multiplier, 'g') if risk_multiplier != 1 else ''}.",
    ]
    return lines


def size_signals(
    signals: list[Signal],
    capital: float,
    available_cash: float,
    cfg: Config,
    risk_multiplier: float = 1.0,
) -> list[Signal]:
    """Fill qty, risk and estimated costs for each signal (planning entry = buy-zone top).

    ``signals`` must already be ranked (``rank_and_select``). TAKE signals use up cash in
    rank order; a TAKE that can't afford 1 share becomes WATCH and its slot passes to the
    next WATCH that was only waiting for a free slot. Other WATCH signals are sized against
    the cash left, for information.
    """
    cash = available_cash
    freed = 0
    for s in signals:
        promotable = s.status == "WATCH" and s.status_note == "no free position slot" and freed > 0
        res = position_size(s.entry_plan, s.stop, capital, cash, cfg, risk_multiplier)
        if not res.ok:
            s.qty, s.risk_amount, s.est_costs = 0, 0.0, None
            if s.status == "TAKE":
                s.status, s.status_note = "WATCH", res.reason or TOO_SMALL
                freed += 1
            else:
                s.warnings.append(res.reason or TOO_SMALL)
            continue
        if promotable:
            s.status, s.status_note = "TAKE", ""
            freed -= 1
        s.qty, s.risk_amount = res.qty, res.risk_amount
        s.est_costs = round_trip_costs(s.entry_plan, s.target, res.qty, cfg.costs, include_slippage=True)
        if res.capped_by == "max_position_pct":
            s.warnings.append(f"size capped at {cfg.account.max_position_pct:g}% of capital")
        elif res.capped_by == "cash":
            s.warnings.append("size limited by available cash")
        if s.status == "TAKE":
            cash -= res.qty * s.entry_plan
    return signals
