from datetime import date

import pytest

from swing_agent.costs import buy_charges, round_trip_costs, sell_charges
from swing_agent.risk import TOO_SMALL, position_size, size_signals
from swing_agent.scanner import Signal


# ---------------------------------------------------------------- costs

def test_round_trip_cost_matches_hand_calculation(cfg):
    c = cfg.costs
    # Buy 100 @ ₹1,000 (turnover 1,00,000):
    #   brokerage 20, STT 100, exchange 2.97, SEBI 0.10, stamp 15, GST 18% × 23.07 = 4.1526 → 142.2226
    b = buy_charges(1000, 100, c)
    assert (b.brokerage, b.stt, b.stamp, b.dp) == (20, pytest.approx(100), pytest.approx(15), 0)
    assert b.total == pytest.approx(142.2226)
    # Sell 100 @ ₹1,100 (turnover 1,10,000):
    #   brokerage 20, STT 110, exchange 3.267, SEBI 0.11, GST 18% × 23.377 = 4.20786, DP 15.93 → 153.51486
    s = sell_charges(1100, 100, c)
    assert s.stamp == 0 and s.dp == pytest.approx(15.93)
    assert s.total == pytest.approx(153.51486)
    assert round_trip_costs(1000, 1100, 100, c, include_slippage=False) == pytest.approx(295.73746)
    # + slippage 0.1% per side: 100 + 110
    assert round_trip_costs(1000, 1100, 100, c) == pytest.approx(505.73746)


def test_zero_qty_costs_nothing(cfg):
    assert buy_charges(100, 0, cfg.costs).total == 0


# ---------------------------------------------------------------- sizing

def test_basic_sizing(cfg):
    r = position_size(entry=100, stop=95, capital=100_000, available_cash=100_000, cfg=cfg)
    assert r.qty == 200 and r.risk_amount == pytest.approx(1000) and r.capped_by is None


def test_position_pct_cap(cfg):
    r = position_size(100, 98, 100_000, 100_000, cfg)   # risk says 500 shares, 20% cap says 200
    assert r.qty == 200 and r.capped_by == "max_position_pct"
    assert r.risk_amount == pytest.approx(400)


def test_cash_cap(cfg):
    r = position_size(100, 95, 100_000, 5_000, cfg)
    assert r.qty == 50 and r.capped_by == "cash"


def test_too_small_rejected(cfg):
    r = position_size(10_000, 8_500, 100_000, 100_000, cfg)   # ₹1,000 budget < ₹1,500 per share
    assert r.qty == 0 and not r.ok and r.reason == TOO_SMALL


def test_caution_multiplier(cfg):
    r = position_size(100, 90, 100_000, 100_000, cfg, risk_multiplier=0.5)
    assert r.qty == 50 and r.risk_budget == pytest.approx(500)


def _sig(sym, entry, stop, status="TAKE", note=""):
    s = Signal(sym, sym, "X", date(2024, 1, 1), entry, entry, entry, stop, entry + 2 * (entry - stop),
               entry - stop, 5, 2, 1, 0, 1, stop)
    s.status, s.status_note = status, note
    return s


def test_size_signals_uses_cash_and_passes_on_freed_slot(cfg):
    sigs = [
        _sig("BIG", 10_000, 8_600),                                   # can't afford 1 share of risk
        _sig("A", 100, 95),                                            # 200 shares = ₹20,000
        _sig("B", 100, 95, "WATCH", "no free position slot"),          # gets BIG's slot
        _sig("C", 100, 95, "WATCH", "sector limit (2) reached for X"), # stays WATCH
    ]
    size_signals(sigs, capital=100_000, available_cash=30_000, cfg=cfg)
    st = {s.symbol: (s.status, s.qty) for s in sigs}
    assert st["BIG"] == ("WATCH", 0) and sigs[0].status_note == TOO_SMALL
    assert st["A"] == ("TAKE", 200)
    assert st["B"] == ("TAKE", 100)          # only ₹10,000 cash left after A
    assert st["C"][0] == "WATCH"
    assert sigs[1].est_costs > 0
