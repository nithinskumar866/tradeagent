"""Indian equity-delivery trade charges (approximate; verify with your broker).

Per order: brokerage (flat), STT (both sides), exchange transaction charges, SEBI fee,
stamp duty (buy only), GST on brokerage + exchange + SEBI, DP charge (per sell).
Slippage is an optional estimate on top (the backtester models it in fill prices instead).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CostsCfg


@dataclass
class Charges:
    """Breakdown of charges for one order, in INR."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    dp: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        """Sum of all charges."""
        return (self.brokerage + self.stt + self.exchange + self.sebi + self.stamp
                + self.gst + self.dp + self.slippage)


def order_charges(side: str, price: float, qty: int, c: CostsCfg, include_slippage: bool = False) -> Charges:
    """Charges for one BUY or SELL order of ``qty`` shares at ``price``."""
    if qty <= 0:
        return Charges()
    turnover = price * qty
    brokerage = c.brokerage_per_order
    exchange = turnover * c.exchange_txn_pct / 100
    sebi = turnover * c.sebi_fee_pct / 100
    ch = Charges(
        brokerage=brokerage,
        stt=turnover * c.stt_pct / 100,
        exchange=exchange,
        sebi=sebi,
        stamp=turnover * c.stamp_duty_buy_pct / 100 if side == "BUY" else 0.0,
        gst=(brokerage + exchange + sebi) * c.gst_pct / 100,
        dp=c.dp_charge_per_sell if side == "SELL" else 0.0,
        slippage=turnover * c.slippage_pct / 100 if include_slippage else 0.0,
    )
    return ch


def buy_charges(price: float, qty: int, c: CostsCfg, include_slippage: bool = False) -> Charges:
    """Charges for a delivery buy."""
    return order_charges("BUY", price, qty, c, include_slippage)


def sell_charges(price: float, qty: int, c: CostsCfg, include_slippage: bool = False) -> Charges:
    """Charges for a delivery sell."""
    return order_charges("SELL", price, qty, c, include_slippage)


def round_trip_costs(entry: float, exit_: float, qty: int, c: CostsCfg, include_slippage: bool = True) -> float:
    """Total charges for buying at ``entry`` and selling at ``exit_`` (optionally + slippage)."""
    return (buy_charges(entry, qty, c, include_slippage).total
            + sell_charges(exit_, qty, c, include_slippage).total)
