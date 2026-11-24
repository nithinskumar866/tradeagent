from datetime import date

import numpy as np
import pandas as pd
import pytest

from fixtures.patterns import index_series, pullback_series
from fixtures.synthetic import make_ohlcv
from swing_agent.backtest import (
    END_OF_TEST,
    entry_fill,
    exit_fill,
    parse_sweep,
    run_backtest,
    summary_markdown,
    metrics_by_period,
    write_outputs,
)
from swing_agent.exit_manager import STOP_HIT, TARGET_HIT, TIME_EXIT, TREND_EXIT
from swing_agent.indicators import add_all_indicators
from swing_agent.market_filter import compute_regime_series
from swing_agent.metrics import cagr, max_drawdown, split_periods, trade_stats
from swing_agent.scanner import evaluate_row, plan_trade, setup_frame

SLIP = 0.1


# ---------------------------------------------------------------- pure fill rules

def test_entry_fill_rules():
    assert entry_fill(100, 101, 95, SLIP) == pytest.approx(100.1)
    assert entry_fill(102, 101, 95, SLIP) is None      # gapped above the buy zone
    assert entry_fill(95, 101, 95, SLIP) is None       # opened at/below the stop


def test_exit_fill_rules():
    assert exit_fill(STOP_HIT, 98, 95, 110, SLIP) == pytest.approx(95 * 0.999)        # normal stop
    assert exit_fill(STOP_HIT, 90, 95, 110, SLIP) == pytest.approx(90 * 0.999)        # gap-down → open
    assert exit_fill(TARGET_HIT, 105, 95, 110, SLIP) == pytest.approx(110 * 0.999)    # normal target
    assert exit_fill(TARGET_HIT, 115, 95, 110, SLIP) == pytest.approx(115 * 0.999)    # gap-up → open


# ---------------------------------------------------------------- scenario helper

def _scenario(cfg, make_bars):
    """Pullback signal on the last base bar T; ``make_bars(plan, fill, open1)`` returns bars from T+1."""
    base = pullback_series()
    idx_full = index_series("bull", len(base) + 40)
    idx_full.index = pd.bdate_range(base.index[0], periods=len(idx_full), name="date")
    f = setup_frame(add_all_indicators(base, cfg, idx_full["close"]), cfg)
    t_row = f.iloc[-1]
    sig, why = evaluate_row("AAA", t_row, f.index[-1].date(), cfg, check_results_dates=False)
    assert sig is not None, why
    open1 = float(t_row["close"])
    fill = open1 * (1 + SLIP / 100)
    plan, _ = plan_trade(fill, sig.swing_low, sig.atr, cfg)
    rows = make_bars(plan, fill, open1)
    dates = pd.bdate_range(base.index[-1], periods=len(rows) + 1, name="date")[1:]
    extra = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=dates)
    extra["volume"] = 1_000_000
    df = pd.concat([base, extra])
    idx = idx_full.loc[: df.index[-1]]
    frames = {"AAA": setup_frame(add_all_indicators(df, cfg, idx["close"]), cfg)}
    meta = pd.DataFrame({"symbol": ["AAA"], "name": ["A"], "sector": ["IT"]})
    res = run_backtest(frames, compute_regime_series(idx, None, cfg), meta, cfg)
    return res, plan, fill, dates


def _flat(price, n):
    return [(price, price * 1.002, price * 0.998, price)] * n


def test_gap_down_through_stop_fills_at_open(cfg):
    def bars(plan, fill, o1):
        gap = plan.stop * 0.95
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002), (gap, gap * 1.01, gap * 0.99, gap)]
    res, plan, fill, dates = _scenario(cfg, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == STOP_HIT and t.entry_price == pytest.approx(fill)
    assert t.exit_price == pytest.approx(plan.stop * 0.95 * 0.999)
    assert t.r_multiple < -1


def test_gap_up_through_target_fills_at_open(cfg):
    def bars(plan, fill, o1):
        g = plan.target * 1.03
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002), (g, g * 1.01, g * 0.995, g)]
    res, plan, *_ = _scenario(cfg, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == TARGET_HIT
    assert t.exit_price == pytest.approx(plan.target * 1.03 * 0.999)


def test_stop_and_target_same_bar_stop_wins(cfg):
    def bars(plan, fill, o1):
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002),
                (o1, plan.target * 1.01, plan.stop * 0.99, o1)]
    res, plan, *_ = _scenario(cfg, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == STOP_HIT and t.exit_price == pytest.approx(plan.stop * 0.999)


def test_gap_above_zone_is_skipped(cfg):
    def bars(plan, fill, o1):
        g = o1 * 1.05
        return [(g, g * 1.01, g * 0.99, g)] + _flat(g, 3)
    res, *_ = _scenario(cfg, bars)
    assert res.trades.empty


def test_breakeven_applies_from_next_bar(cfg):
    def bars(plan, fill, o1):
        up = fill + plan.r * 1.2                       # ≥ 1R but below the 2R target
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002),
                (up * 0.99, up * 1.002, fill * 1.001, up),  # low stays above entry: no stop today
                (up, up * 1.001, fill * 0.995, fill * 1.01)]  # dips through entry → stopped at breakeven
    res, plan, fill, _ = _scenario(cfg, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == STOP_HIT
    assert t.exit_price == pytest.approx(fill * 0.999)


def test_trend_exit_fills_next_open(cfg):
    # In this synthetic uptrend the 50 EMA sits below the stop, so use a 10-bar trend EMA
    # (above the stop) to get a close that is under the trend EMA but still above the stop.
    c = cfg.model_copy(deep=True)
    c.stops_targets.trend_exit_ema = 10

    def bars(plan, fill, o1):
        below = (plan.stop + fill) / 2
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002),
                (o1, o1 * 1.001, below * 0.999, below),
                (below * 1.01, below * 1.02, below * 1.005, below * 1.015)]
    res, plan, fill, dates = _scenario(c, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == TREND_EXIT and t.exit_date == dates[2].date()
    assert t.exit_price == pytest.approx((plan.stop + fill) / 2 * 1.01 * 0.999)


def test_time_exit_after_time_stop_days(cfg):
    def bars(plan, fill, o1):
        p = fill * 1.01                                 # drifts sideways above entry, below 1R
        return [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002)] + _flat(p, cfg.stops_targets.time_stop_days + 3)
    res, plan, fill, dates = _scenario(cfg, bars)
    t = res.trades.iloc[0]
    assert t.exit_reason == TIME_EXIT
    assert t.exit_date == dates[cfg.stops_targets.time_stop_days + 1].date()


def test_open_position_closed_at_end(cfg):
    res, *_ = _scenario(cfg, lambda plan, fill, o1: [(o1, o1 * 1.003, o1 * 0.999, o1 * 1.002)] + _flat(fill * 1.01, 2))
    assert res.trades.iloc[0].exit_reason == END_OF_TEST
    assert res.equity["positions"].iloc[-1] == 0


# ---------------------------------------------------------------- integration

def test_three_year_portfolio_run_respects_limits(cfg, tmp_path):
    c = cfg.model_copy(deep=True)
    c.account.max_open_positions = 3
    c.account.max_per_sector = 1
    n = 760
    idx = make_ohlcv(n, seed=99, start_price=10000, drift=0.0006, vol=0.008).set_index("date")
    sectors = ["IT", "Bank", "Pharma"]
    frames, rows = {}, []
    for i in range(10):
        sym = f"S{i}"
        df = make_ohlcv(n, seed=i, start_price=500, drift=0.0008, vol=0.018).set_index("date")
        frames[sym] = setup_frame(add_all_indicators(df, c, idx["close"]), c)
        rows.append((sym, sym, sectors[i % 3]))
    meta = pd.DataFrame(rows, columns=["symbol", "name", "sector"])
    res = run_backtest(frames, compute_regime_series(idx, None, c), meta, c)

    assert len(res.trades) > 0
    assert not res.equity["equity"].isna().any()
    assert res.equity["positions"].max() <= 3
    sec = dict(zip(meta.symbol, meta.sector))
    for held in res.equity["held"]:
        names = [h for h in held.split(",") if h]
        counts = pd.Series([sec[h] for h in names]).value_counts()
        assert counts.empty or counts.max() <= 1
    final = res.equity["equity"].iloc[-1]
    assert final == pytest.approx(c.backtest.initial_capital + res.trades["pnl"].sum())

    periods = write_outputs(res, c, tmp_path / "bt")
    assert {"trades.csv", "equity.csv", "summary.md"} <= {p.name for p in (tmp_path / "bt").iterdir()}
    text = (tmp_path / "bt" / "summary.md").read_text(encoding="utf-8")
    assert "Survivorship bias" in text and "out-of-sample" in text and "Results-date filters are ignored" in text
    assert "Full period" in periods


# ---------------------------------------------------------------- metrics + sweep parsing

def test_metric_helpers():
    eq = pd.Series([100, 110, 99, 120, 108], index=pd.bdate_range("2024-01-01", periods=5))
    dd, days = max_drawdown(eq)
    assert dd == pytest.approx(-10.0)
    assert days == (eq.index[3] - eq.index[2]).days
    assert cagr(100, 121, pd.Timestamp("2020-01-01"), pd.Timestamp("2022-01-01")) == pytest.approx(10, abs=0.05)
    tr = pd.DataFrame({"pnl": [100, -50, -50, 200], "r_multiple": [1, -0.5, -0.5, 2],
                       "exit_date": pd.bdate_range("2024-01-01", periods=4), "bars_held": [3, 4, 5, 6]})
    s = trade_stats(tr)
    assert s["win_rate_pct"] == 50 and s["profit_factor"] == pytest.approx(3)
    assert s["expectancy_r"] == pytest.approx(0.5) and s["longest_losing_streak"] == 2


def test_split_periods():
    idx = pd.bdate_range("2021-12-20", periods=20)
    eq = pd.DataFrame({"equity": np.linspace(100, 120, 20), "benchmark": 100.0, "positions": 1}, index=idx)
    tr = pd.DataFrame({"entry_date": [date(2021, 12, 21), date(2022, 1, 5)], "pnl": [1, 2]})
    parts = split_periods(eq, tr, date(2021, 12, 31))
    assert list(parts) == ["In-sample", "Out-of-sample", "Full period"]
    assert parts["Out-of-sample"][0].index[0] == pd.Timestamp("2021-12-31")
    assert len(parts["In-sample"][1]) == 1 and len(parts["Out-of-sample"][1]) == 1


def test_parse_sweep(cfg):
    grid = parse_sweep(["target_r=1.5,2", "entry.rsi_dip_max=45,50"], cfg)
    assert grid == [("stops_targets", "target_r", [1.5, 2.0]), ("entry", "rsi_dip_max", [45.0, 50.0])]
    with pytest.raises(ValueError, match="Unknown"):
        parse_sweep(["nope=1"], cfg)
