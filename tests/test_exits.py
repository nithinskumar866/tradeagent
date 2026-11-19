from datetime import date

import pandas as pd
import pytest

from fixtures.patterns import index_series
from swing_agent.exit_manager import (
    HOLD,
    MOVE_STOP,
    RESULTS_SOON,
    STOP_HIT,
    TARGET_HIT,
    TIME_EXIT,
    TREND_EXIT,
    check_position,
    exit_rule,
    sort_alerts,
)
from swing_agent.indicators import add_all_indicators
from swing_agent.market_filter import compute_regime_series, regime_at
from swing_agent.portfolio import Lot, Position


def rule(cfg, low=99, high=104, close=102, ema_exit=95, entry=100, stop=95, target=110, r=5, held=3,
         results=False):
    return exit_rule(low, high, close, ema_exit, entry, stop, target, r, held, cfg, results)


def test_hold(cfg):
    assert rule(cfg) == HOLD


def test_stop_hit(cfg):
    assert rule(cfg, low=94.5) == STOP_HIT


def test_gap_down_through_stop_is_stop_hit(cfg):
    assert rule(cfg, low=88, high=90, close=89) == STOP_HIT


def test_target_hit(cfg):
    assert rule(cfg, high=110.5, close=109) == TARGET_HIT


def test_gap_up_through_target_is_target_hit(cfg):
    assert rule(cfg, low=112, high=115, close=114) == TARGET_HIT


def test_stop_and_target_same_bar_stop_wins(cfg):
    assert rule(cfg, low=94, high=111) == STOP_HIT


def test_breakeven_move(cfg):
    assert rule(cfg, close=105) == MOVE_STOP            # entry + 1R
    assert rule(cfg, low=101, close=105, stop=100) == HOLD        # already at breakeven


def test_trend_exit(cfg):
    assert rule(cfg, close=101, ema_exit=101.5) == TREND_EXIT


def test_time_exit(cfg):
    assert rule(cfg, held=cfg.stops_targets.time_stop_days) == TIME_EXIT
    assert rule(cfg, held=cfg.stops_targets.time_stop_days - 1) == HOLD


def test_results_soon(cfg):
    assert rule(cfg, results=True) == RESULTS_SOON


def test_priority_move_stop_before_trend(cfg):
    assert rule(cfg, close=105, ema_exit=106) == MOVE_STOP


def _position(entry_date, entry=100.0, stop=95.0, target=110.0, qty=10):
    return Position("AAA", [Lot(1, "AAA", entry_date, entry, qty, 0.0, stop, stop, target, None)])


def test_check_position_live(cfg):
    idx = index_series("bull", 300)
    px = idx.copy()
    px[["open", "high", "low", "close"]] /= 100   # use the index as a price series
    frame = add_all_indicators(px, cfg)
    entry_day = frame.index[-6].date()
    last = frame.iloc[-1]
    pos = _position(entry_day, entry=float(last["close"]) - 1, stop=float(last["low"]) - 5,
                    target=float(last["high"]) + 50)
    regime = regime_at(compute_regime_series(index_series("bear", 300), None, cfg), frame.index[-1], cfg)
    st = check_position(pos, frame, frame.index[-1].date(), cfg, results_dates=None, regime=regime)
    assert st.days_held == 5
    assert st.pnl == pytest.approx(10.0)
    assert st.r_now == pytest.approx(1 / (pos.avg_entry - pos.stop_initial))
    assert "Market weak. Consider tightening stops." in st.notes
    assert st.action == HOLD and st.alert is None


def test_check_position_results_soon_and_no_stop(cfg):
    idx = index_series("bull", 300)
    frame = add_all_indicators(idx / 100, cfg)
    asof = frame.index[-1].date()
    last = frame.iloc[-1]
    pos = _position(frame.index[-3].date(), entry=float(last["close"]), stop=float(last["low"]) - 5,
                    target=float(last["high"]) + 50)
    st = check_position(pos, frame, asof, cfg, results_dates=[asof + pd.Timedelta(days=2)])
    assert st.action == RESULTS_SOON and "results on" in st.alert.message
    pos.lots[0].stop = None
    assert check_position(pos, frame, asof, cfg).action == "DATA"


def test_alert_sorting(cfg):
    from swing_agent.exit_manager import Alert

    alerts = [Alert("A", MOVE_STOP, "", ""), Alert("B", "REGIME_CHANGE", "", ""), Alert("C", STOP_HIT, "", "")]
    assert [a.type for a in sort_alerts(alerts)] == [STOP_HIT, MOVE_STOP, "REGIME_CHANGE"]
