"""Trade log → open positions, closed trades, cash and capital (FIFO, partial sells allowed).

Positions are never stored: they are rebuilt from the ``trades`` table every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


class PortfolioError(Exception):
    """Raised for an inconsistent trade log (e.g. selling more than is held)."""


@dataclass
class Lot:
    """The still-open part of one BUY trade."""

    trade_id: int
    symbol: str
    date: date
    price: float
    qty: int
    fee_per_share: float
    stop: float | None
    stop_initial: float | None
    target: float | None
    signal_id: int | None


@dataclass
class Position:
    """An open position (one symbol), built from its open lots."""

    symbol: str
    lots: list[Lot] = field(default_factory=list)

    @property
    def qty(self) -> int:
        """Total shares held."""
        return sum(l.qty for l in self.lots)

    @property
    def avg_entry(self) -> float:
        """Quantity-weighted average entry price."""
        return sum(l.price * l.qty for l in self.lots) / self.qty

    @property
    def cost_basis(self) -> float:
        """Money tied up: entry value plus the buy fees of the open shares."""
        return sum((l.price + l.fee_per_share) * l.qty for l in self.lots)

    @property
    def entry_date(self) -> date:
        """Date of the oldest open lot."""
        return min(l.date for l in self.lots)

    @property
    def stop(self) -> float | None:
        """Current stop: from the most recent open lot that has one."""
        for l in reversed(self.lots):
            if l.stop is not None:
                return l.stop
        return None

    @property
    def stop_initial(self) -> float | None:
        """Quantity-weighted stop at entry (defines 1R)."""
        lots = [l for l in self.lots if l.stop_initial is not None]
        if not lots:
            return self.stop
        return sum(l.stop_initial * l.qty for l in lots) / sum(l.qty for l in lots)

    @property
    def target(self) -> float | None:
        """Target from the most recent open lot that has one."""
        for l in reversed(self.lots):
            if l.target is not None:
                return l.target
        return None

    @property
    def r_per_share(self) -> float | None:
        """Initial risk per share (entry − initial stop)."""
        si = self.stop_initial
        if si is None or self.avg_entry - si <= 0:
            return None
        return self.avg_entry - si


@dataclass
class ClosedTrade:
    """A FIFO-matched slice of a buy lot closed by a sell."""

    symbol: str
    buy_trade_id: int
    sell_trade_id: int
    entry_date: date
    exit_date: date
    qty: int
    entry_price: float
    exit_price: float
    fees: float
    stop_initial: float | None

    @property
    def pnl_gross(self) -> float:
        """Price P&L before charges."""
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def pnl_net(self) -> float:
        """P&L after buy and sell charges."""
        return self.pnl_gross - self.fees

    @property
    def r_multiple(self) -> float | None:
        """Net P&L per share in units of initial risk."""
        if self.stop_initial is None or self.entry_price - self.stop_initial <= 0:
            return None
        return (self.pnl_net / self.qty) / (self.entry_price - self.stop_initial)

    @property
    def holding_days(self) -> int:
        """Calendar days held."""
        return (self.exit_date - self.entry_date).days


@dataclass
class Portfolio:
    """Current state derived from the trade log."""

    initial_capital: float
    positions: dict[str, Position]
    closed: list[ClosedTrade]

    @property
    def realised_pnl(self) -> float:
        """Net realised P&L of all closed trades."""
        return sum(c.pnl_net for c in self.closed)

    @property
    def capital(self) -> float:
        """Live capital = starting capital + realised net P&L."""
        return self.initial_capital + self.realised_pnl

    @property
    def invested(self) -> float:
        """Cost of open positions (including their buy fees)."""
        return sum(p.cost_basis for p in self.positions.values())

    @property
    def available_cash(self) -> float:
        """Capital not tied up in open positions."""
        return self.capital - self.invested

    def sector_counts(self, sectors: dict[str, str]) -> dict[str, int]:
        """Open positions per sector."""
        out: dict[str, int] = {}
        for sym in self.positions:
            sec = sectors.get(sym, "Unknown")
            out[sec] = out.get(sec, 0) + 1
        return out


def _opt(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def build_portfolio(trades: pd.DataFrame, initial_capital: float) -> Portfolio:
    """Replay the trade log in (date, id) order with FIFO matching of sells to buy lots."""
    open_lots: dict[str, list[Lot]] = {}
    closed: list[ClosedTrade] = []
    if not trades.empty:
        trades = trades.sort_values(["date", "id"])
    for t in trades.itertuples(index=False):
        d = date.fromisoformat(str(t.date))
        qty = int(t.qty)
        fees = float(t.fees or 0.0)
        if t.side == "BUY":
            open_lots.setdefault(t.symbol, []).append(Lot(
                trade_id=int(t.id), symbol=t.symbol, date=d, price=float(t.price), qty=qty,
                fee_per_share=fees / qty if qty else 0.0,
                stop=_opt(t.stop), stop_initial=_opt(getattr(t, "stop_initial", None)) or _opt(t.stop),
                target=_opt(t.target), signal_id=None if pd.isna(t.signal_id) else int(t.signal_id),
            ))
            continue
        lots = open_lots.get(t.symbol, [])
        held = sum(l.qty for l in lots)
        if qty > held:
            raise PortfolioError(f"Trade {t.id}: selling {qty} {t.symbol} but only {held} held on {d}.")
        sell_fee_ps = fees / qty if qty else 0.0
        remaining = qty
        while remaining:
            lot = lots[0]
            take = min(remaining, lot.qty)
            closed.append(ClosedTrade(
                symbol=t.symbol, buy_trade_id=lot.trade_id, sell_trade_id=int(t.id),
                entry_date=lot.date, exit_date=d, qty=take, entry_price=lot.price,
                exit_price=float(t.price), fees=(lot.fee_per_share + sell_fee_ps) * take,
                stop_initial=lot.stop_initial,
            ))
            lot.qty -= take
            remaining -= take
            if lot.qty == 0:
                lots.pop(0)
    positions = {s: Position(s, lots) for s, lots in open_lots.items() if lots}
    return Portfolio(initial_capital, positions, closed)
