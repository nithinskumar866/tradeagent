from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from fixtures.patterns import downtrend_series, index_series, pullback_series
from fixtures.synthetic import make_ohlcv
from swing_agent.indicators import add_all_indicators
from swing_agent.market_filter import compute_regime_series, regime_at
from swing_agent.scanner import (
    Signal,
    evaluate_row,
    first_failed_condition,
    plan_trade,
    rank_and_select,
    scan,
    setup_frame,
)

IDX = index_series("bull", 400)


def _frame(df, cfg, idx=IDX):
    return setup_frame(add_all_indicators(df, cfg, idx["close"]), cfg)


# ---------------------------------------------------------------- entry rules

def test_perfect_pullback_triggers(cfg):
    f = _frame(pullback_series(), cfg)
    row = f.iloc[-1]
    assert bool(row["setup"]), first_failed_condition(row)
    assert not f["setup"].iloc[:-1].tail(10).any()  # only the trigger bar fires

    sig, why = evaluate_row("AAA", row, f.index[-1].date(), cfg, "Aaa Ltd", "IT", results_dates=None)
    assert why is None
    assert sig.buy_zone_low == pytest.approx(row["close"])
    assert sig.buy_zone_high == pytest.approx(row["high"] * 1.005)
    assert sig.stop < sig.buy_zone_low
    assert sig.target == pytest.approx(sig.buy_zone_high + 2 * (sig.buy_zone_high - sig.stop))
    assert sig.rr == pytest.approx(2.0)
    assert len(sig.reasons) >= 5 and "Pulled back" in sig.reasons[1]
    assert "results date unknown" in sig.warnings
    assert row["bonus"] == 2 and sig.score == pytest.approx(row["rs"] + 4)


def test_downtrend_does_not_trigger(cfg):
    row = _frame(downtrend_series(), cfg).iloc[-1]
    assert not row["setup"] and first_failed_condition(row) == "uptrend"


def test_too_deep_pullback_does_not_trigger(cfg):
    row = _frame(pullback_series(depth_steps=9, step_pct=2.0), cfg).iloc[-1]
    assert not row["setup"]
    assert row["pullback_pct"] > cfg.entry.pullback_max_pct
    assert not row["c_pullback"]


def test_broken_ema50_does_not_trigger(cfg):
    row = _frame(pullback_series(depth_steps=7, step_pct=1.5, trigger_pct=0.5), cfg).iloc[-1]
    assert not row["setup"]
    assert row["close"] < row["ema_m"]
    assert not row["c_touch"]


def test_universe_filter_rejects_illiquid_and_cheap(cfg):
    df = pullback_series()
    thin = df.assign(volume=1000)
    assert not _frame(thin, cfg).iloc[-1]["c_universe"]
    cheap = df.copy()
    cheap[["open", "high", "low", "close"]] /= 20  # under ₹100
    assert not _frame(cheap, cfg).iloc[-1]["c_universe"]


# ---------------------------------------------------------------- no look-ahead

def test_no_lookahead(cfg):
    rng = np.random.default_rng(42)
    df = make_ohlcv(600, seed=11, drift=0.0008, vol=0.015).set_index("date")
    idx = make_ohlcv(600, seed=12, start_price=10000).set_index("date")
    full = setup_frame(add_all_indicators(df, cfg, idx["close"]), cfg)
    for t in rng.integers(260, 599, size=8):
        part = setup_frame(add_all_indicators(df.iloc[: t + 1], cfg, idx["close"].iloc[: t + 1]), cfg)
        pd.testing.assert_series_equal(part.iloc[-1], full.iloc[t], check_names=False)


# ---------------------------------------------------------------- trade plan

def test_plan_widens_stop_to_atr(cfg):
    plan, why = plan_trade(entry=100, swing_low=99.5, atr=3, cfg=cfg)
    assert why is None and plan.stop_widened_to_atr
    assert plan.stop == pytest.approx(97) and plan.target == pytest.approx(106)


def test_plan_rejects_wide_stop(cfg):
    plan, why = plan_trade(entry=100, swing_low=85, atr=2, cfg=cfg)
    assert plan is None and "stop too wide" in why


def test_plan_normal_case(cfg):
    plan, _ = plan_trade(entry=100, swing_low=95, atr=2, cfg=cfg)
    assert plan.stop == pytest.approx(95 * 0.995)
    assert plan.r == pytest.approx(100 - 95 * 0.995)


def test_results_soon_blocks_and_later_warns(cfg):
    f = _frame(pullback_series(), cfg)
    row, asof = f.iloc[-1], f.index[-1].date()
    sig, why = evaluate_row("AAA", row, asof, cfg, results_dates=[asof + timedelta(days=3)])
    assert sig is None and "results on" in why
    sig, why = evaluate_row("AAA", row, asof, cfg, results_dates=[asof + timedelta(days=20)])
    assert sig is not None and any("results on" in w for w in sig.warnings)
    sig, _ = evaluate_row("AAA", row, asof, cfg, results_dates=[asof + timedelta(days=3)],
                          check_results_dates=False)
    assert sig is not None


# ---------------------------------------------------------------- scan + ranking

def _sig(sym, sector, score):
    return Signal(sym, sym, sector, date(2024, 1, 1), 100, 100, 101, 95, 113, 6, 6, 2, score, 0, 2, 96)


def test_rank_and_select_respects_slots_sector_and_holdings():
    sigs = [_sig("A", "IT", 9), _sig("B", "IT", 8), _sig("C", "IT", 7), _sig("D", "Bank", 6),
            _sig("E", "Pharma", 5), _sig("F", "Auto", 4)]
    ranked = rank_and_select(sigs, max_positions=4, held={"D"}, open_sector_counts={"Bank": 1},
                             max_per_sector=2)
    status = {s.symbol: (s.status, s.status_note) for s in ranked}
    assert [s.symbol for s in ranked] == ["A", "B", "C", "D", "E", "F"]
    assert status["A"][0] == status["B"][0] == "TAKE"
    assert status["C"] == ("WATCH", "sector limit (2) reached for IT")
    assert status["D"] == ("WATCH", "already held")
    assert status["E"][0] == "TAKE"          # 3 free slots (4 − 1 held): A, B, E
    assert status["F"] == ("WATCH", "no free position slot")


def test_scan_blocks_entries_in_bearish_regime(cfg):
    df = pullback_series()
    frames = {"AAA": _frame(df, cfg)}
    meta = pd.DataFrame({"symbol": ["AAA"], "name": ["A"], "sector": ["IT"]})
    asof = df.index[-1].date()

    bull = regime_at(compute_regime_series(IDX, None, cfg), asof, cfg)
    res = scan(frames, asof, bull, cfg, meta)
    assert [s.symbol for s in res.signals] == ["AAA"] and res.funnel["passed"] == 1

    bear_idx = index_series("bear", 400)
    bear = regime_at(compute_regime_series(bear_idx, None, cfg), asof, cfg)
    res = scan(frames, asof, bear, cfg, meta)
    assert res.signals == [] and "BEARISH" in res.rejected[0][1]


def test_scan_marks_missing_bar_as_stale(cfg):
    df = pullback_series()
    frames = {"AAA": _frame(df.iloc[:-1], cfg)}
    meta = pd.DataFrame({"symbol": ["AAA"], "name": ["A"], "sector": ["IT"]})
    asof = df.index[-1].date()
    bull = regime_at(compute_regime_series(IDX, None, cfg), asof, cfg)
    assert scan(frames, asof, bull, cfg, meta).stale == ["AAA"]
