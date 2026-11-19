"""Logging the user's own trades (used by the CLI and the dashboard). Never talks to a broker."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import db
from .config import Config
from .costs import buy_charges, sell_charges
from .fmt import inr
from .portfolio import build_portfolio


class TradeError(Exception):
    """Raised for an invalid trade entry."""


@dataclass
class TradeLogged:
    """Result of logging a trade."""

    trade_id: int
    messages: list[str] = field(default_factory=list)


def log_buy(
    conn: sqlite3.Connection,
    cfg: Config,
    symbol: str,
    price: float,
    qty: int,
    trade_date: date | None = None,
    stop: float | None = None,
    target: float | None = None,
    fees: float | None = None,
    note: str | None = None,
) -> TradeLogged:
    """Record a BUY. Stop/target default to the latest signal for the symbol; the target is
    re-based on the actual fill (fill + target_r × (fill − stop))."""
    symbol = symbol.strip().upper()
    if price <= 0 or qty <= 0:
        raise TradeError("Price and quantity must be positive.")
    msgs: list[str] = []
    known = set(db.get_symbols(conn, active_only=False)["symbol"])
    if symbol not in known:
        msgs.append(f"Note: {symbol} is not in the watchlist; exit checks need its price data.")

    signal_id = None
    if stop is None:
        sig = db.latest_signal(conn, symbol)
        if sig is not None:
            stop, signal_id = float(sig["stop"]), int(sig["id"])
            msgs.append(f"Using stop {inr(stop)} from signal #{signal_id} ({sig['run_date']}).")
    if stop is None:
        raise TradeError(f"No signal found for {symbol}; give the stop-loss with --stop.")
    if stop >= price:
        raise TradeError(f"Stop {stop} must be below the buy price {price}.")
    if target is None:
        target = price + cfg.stops_targets.target_r * (price - stop)
        msgs.append(f"Target set to {inr(target)} ({cfg.stops_targets.target_r:g}R from your fill).")
    if fees is None:
        fees = buy_charges(price, qty, cfg.costs).total
        msgs.append(f"Estimated buy charges {inr(fees)} (override with --fees).")

    pf = build_portfolio(db.get_trades(conn), cfg.account.capital)
    if price * qty + fees > pf.available_cash + 1e-6:
        msgs.append(f"Warning: this uses more than the available cash ({inr(pf.available_cash, 0)}).")

    tid = db.insert_trade(conn, symbol, "BUY", trade_date or date.today(), price, qty, fees,
                          stop=stop, target=target, signal_id=signal_id, note=note)
    return TradeLogged(tid, msgs)


def log_sell(
    conn: sqlite3.Connection,
    cfg: Config,
    symbol: str,
    price: float,
    qty: int,
    trade_date: date | None = None,
    fees: float | None = None,
    note: str | None = None,
) -> TradeLogged:
    """Record a SELL (partial sells allowed; matched FIFO against open buys)."""
    symbol = symbol.strip().upper()
    if price <= 0 or qty <= 0:
        raise TradeError("Price and quantity must be positive.")
    pf = build_portfolio(db.get_trades(conn), cfg.account.capital)
    pos = pf.positions.get(symbol)
    held = pos.qty if pos else 0
    if qty > held:
        raise TradeError(f"You hold {held} {symbol}; can't sell {qty}.")
    d = trade_date or date.today()
    if pos and d < pos.entry_date:
        raise TradeError(f"Sell date {d} is before the buy date {pos.entry_date}.")
    msgs: list[str] = []
    if fees is None:
        fees = sell_charges(price, qty, cfg.costs).total
        msgs.append(f"Estimated sell charges {inr(fees)} (override with --fees).")
    tid = db.insert_trade(conn, symbol, "SELL", d, price, qty, fees, note=note)
    after = build_portfolio(db.get_trades(conn), cfg.account.capital)
    pnl = sum(c.pnl_net for c in after.closed if c.sell_trade_id == tid)
    msgs.append(f"Realised P&L on this sale: {inr(pnl)} after charges.")
    return TradeLogged(tid, msgs)


def move_stop(conn: sqlite3.Connection, cfg: Config, symbol: str, new_stop: float) -> list[str]:
    """Update the current stop of an open position (R stays based on the original stop)."""
    symbol = symbol.strip().upper()
    pf = build_portfolio(db.get_trades(conn), cfg.account.capital)
    pos = pf.positions.get(symbol)
    if pos is None:
        raise TradeError(f"No open position in {symbol}.")
    if new_stop <= 0:
        raise TradeError("Stop must be positive.")
    old = pos.stop
    db.update_stop(conn, [l.trade_id for l in pos.lots], new_stop)
    return [f"{symbol}: stop moved from {inr(old)} to {inr(new_stop)}."]
