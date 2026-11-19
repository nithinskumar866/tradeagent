from datetime import date

import pandas as pd
import pytest

from swing_agent import db
from swing_agent.portfolio import PortfolioError, build_portfolio
from swing_agent.trading import TradeError, log_buy, log_sell, move_stop


def _trades(rows):
    cols = ["id", "symbol", "side", "date", "price", "qty", "stop", "target", "fees", "signal_id", "note",
            "stop_initial"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def test_fifo_partial_sells_cash_and_capital():
    t = _trades([
        (1, "AAA", "BUY", "2024-01-01", 100.0, 100, 95.0, 110.0, 10.0, None, None, 95.0),
        (2, "AAA", "BUY", "2024-01-05", 110.0, 50, 104.0, 122.0, 5.0, None, None, 104.0),
        (3, "AAA", "SELL", "2024-01-10", 120.0, 120, None, None, 12.0, None, None, None),
    ])
    pf = build_portfolio(t, 100_000)
    first, second = pf.closed
    # Lot 1 fully closed: gross 2,000; fees 10 + 12×100/120 = 20 → net 1,980; R = 19.8 / 5
    assert (first.qty, first.pnl_gross, first.pnl_net) == (100, pytest.approx(2000), pytest.approx(1980))
    assert first.r_multiple == pytest.approx(3.96)
    # 20 of lot 2: gross 200; fees 5×20/50 + 12×20/120 = 4 → net 196
    assert (second.qty, second.pnl_net) == (20, pytest.approx(196))
    pos = pf.positions["AAA"]
    assert pos.qty == 30 and pos.avg_entry == pytest.approx(110)
    assert pf.realised_pnl == pytest.approx(2176)
    assert pf.capital == pytest.approx(102_176)
    assert pf.invested == pytest.approx(30 * 110.1)
    assert pf.available_cash == pytest.approx(102_176 - 3303)


def test_full_exit_removes_position():
    t = _trades([
        (1, "AAA", "BUY", "2024-01-01", 100.0, 10, 95.0, None, 0.0, None, None, 95.0),
        (2, "AAA", "SELL", "2024-01-02", 90.0, 10, None, None, 0.0, None, None, None),
    ])
    pf = build_portfolio(t, 1000)
    assert pf.positions == {} and pf.realised_pnl == pytest.approx(-100)
    assert pf.closed[0].r_multiple == pytest.approx(-2)


def test_overselling_is_an_error():
    t = _trades([
        (1, "AAA", "BUY", "2024-01-01", 100.0, 10, 95.0, None, 0.0, None, None, 95.0),
        (2, "AAA", "SELL", "2024-01-02", 90.0, 11, None, None, 0.0, None, None, None),
    ])
    with pytest.raises(PortfolioError):
        build_portfolio(t, 1000)


# ---------------------------------------------------------------- trade logging (DB)

def _signal(conn, symbol="AAA", stop=95.0, target=115.0):
    return db.replace_run_outputs(conn, "2024-01-01", [{
        "symbol": symbol, "setup": "X", "signal_close": 100, "buy_zone_low": 100, "buy_zone_high": 101,
        "stop": stop, "target": target, "qty": 10, "risk_amount": 50, "rr": 2, "score": 1,
        "reasons_json": "[]", "warnings_json": "[]", "regime": "BULLISH", "status": "TAKE", "status_note": ""}],
        [])[0]


def test_log_buy_takes_stop_from_signal_and_rebases_target(conn, cfg):
    sid = _signal(conn)
    res = log_buy(conn, cfg, "aaa", 102.0, 10, date(2024, 1, 2))
    row = db.get_trades(conn).iloc[0]
    assert row["symbol"] == "AAA" and row["signal_id"] == sid
    assert row["stop"] == 95.0 and row["stop_initial"] == 95.0
    assert row["target"] == pytest.approx(102 + 2 * 7)
    assert row["fees"] > 20  # estimated charges
    assert any("signal" in m for m in res.messages)


def test_log_buy_needs_a_stop(conn, cfg):
    with pytest.raises(TradeError, match="--stop"):
        log_buy(conn, cfg, "ZZZ", 100.0, 1)
    with pytest.raises(TradeError, match="below"):
        log_buy(conn, cfg, "ZZZ", 100.0, 1, stop=101.0)


def test_sell_checks_holding_and_move_stop_keeps_r(conn, cfg):
    log_buy(conn, cfg, "AAA", 100.0, 10, date(2024, 1, 2), stop=95.0, fees=0)
    with pytest.raises(TradeError, match="hold 10"):
        log_sell(conn, cfg, "AAA", 105.0, 11, date(2024, 1, 3))
    move_stop(conn, cfg, "AAA", 100.0)
    pos = build_portfolio(db.get_trades(conn), cfg.account.capital).positions["AAA"]
    assert pos.stop == 100.0 and pos.stop_initial == 95.0 and pos.r_per_share == pytest.approx(5)
    res = log_sell(conn, cfg, "AAA", 105.0, 4, date(2024, 1, 3), fees=0)
    assert any("₹20.00" in m for m in res.messages)
    pf = build_portfolio(db.get_trades(conn), cfg.account.capital)
    assert pf.positions["AAA"].qty == 6
