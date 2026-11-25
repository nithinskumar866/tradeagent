from datetime import date

import pandas as pd
import pytest

from fixtures.patterns import downtrend_series, index_series, pullback_series
from fixtures.synthetic import FakeSource
from swing_agent import db
from swing_agent.analyze import (
    AnalyzeError,
    TargetHistory,
    add_to_watchlist,
    analyze_symbol,
    clean_symbol,
    estimate_target,
    format_analysis,
    target_history,
)
from swing_agent.watchlist import load_watchlist


@pytest.fixture
def setup_db(conn, cfg, tmp_path):
    """DB with Nifty/VIX, a pullback setup (AAA, in watchlist) and a source that also knows DOWN."""
    pb = pullback_series()
    n = len(pb)
    idx = index_series("bull", n)
    vix = idx.assign(open=15.0, high=15.0, low=15.0, close=15.0)
    down = downtrend_series(n - 1)  # returns n bars (one extra bounce bar)
    down.index = pb.index
    down[["open", "high", "low", "close"]] *= 0.5
    for sym, df in {"^NSEI": idx, "^INDIAVIX": vix, "AAA": pb}.items():
        db.upsert_prices(conn, sym, df.reset_index())
    wl = tmp_path / "wl.csv"
    wl.write_text("symbol,name,sector\nAAA,Aaa Ltd,IT\n", encoding="utf-8")
    c = cfg.model_copy(deep=True)
    c.universe.watchlist_file = str(wl)
    c.data.fetch_results_dates = False
    db.upsert_symbols(conn, load_watchlist(wl))
    src = FakeSource({"DOWN": down.reset_index(), "AAA": pb.reset_index()})
    return c, src, tmp_path


def test_clean_symbol():
    assert clean_symbol(" tcs.ns ") == "TCS"
    assert clean_symbol("m&m") == "M&M"
    assert clean_symbol("tcs", "bse") == "TCS.BO"
    assert clean_symbol("TCS.BO") == "TCS.BO"            # typed suffix wins
    assert clean_symbol("TCS.NS", "BSE") == "TCS"
    with pytest.raises(AnalyzeError, match="NSE or BSE"):
        clean_symbol("TCS", "NYSE")
    with pytest.raises(AnalyzeError):
        clean_symbol("TCS; drop table")


def test_setup_stock_gets_rule_plan_and_target_date(conn, setup_db):
    cfg, src, tmp = setup_db
    a = analyze_symbol(conn, cfg, "aaa", source=src, reports_dir=tmp)
    assert a.is_setup and a.plan.kind == "SIGNAL" and a.in_watchlist and not a.downloaded
    assert all(c.passed for c in a.checks)
    assert a.plan.qty > 0 and a.plan.stop < a.plan.entry < a.plan.target
    eta = a.plan.eta
    assert eta.entry_date > a.asof and eta.earliest <= eta.likely <= eta.latest <= eta.time_stop
    text = format_analysis(a)
    assert "BUY SETUP" in text and "Approximate target date" in text and "Not investment advice" in text


def test_other_stock_is_downloaded_and_gets_hypothetical_plan(conn, setup_db):
    cfg, src, tmp = setup_db
    a = analyze_symbol(conn, cfg, "DOWN", source=src, reports_dir=tmp)
    assert a.downloaded and not a.in_watchlist and not a.is_setup
    assert a.plan.kind == "HYPOTHETICAL"
    uptrend = next(c for c in a.checks if c.rule == "Uptrend")
    assert not uptrend.passed and "200 EMA" in uptrend.detail
    assert "not a signal" in format_analysis(a)
    # the symbol is remembered (inactive) but not scanned daily until added
    assert "DOWN" not in set(db.get_symbols(conn)["symbol"])
    assert add_to_watchlist(conn, cfg, "DOWN", "Down Ltd", "Bank")
    assert not add_to_watchlist(conn, cfg, "down")
    assert "DOWN" in set(load_watchlist(cfg.watchlist_path)["symbol"])
    assert "DOWN" in set(db.get_symbols(conn)["symbol"])


def test_unknown_symbol_gives_clear_error(conn, setup_db):
    cfg, src, tmp = setup_db
    with pytest.raises(AnalyzeError, match="Couldn't get prices"):
        analyze_symbol(conn, cfg, "NOPE", source=src, reports_dir=tmp)
    with pytest.raises(AnalyzeError, match="Allow downloading"):
        analyze_symbol(conn, cfg, "DOWN", allow_download=False, source=src, reports_dir=tmp)


# ---------------------------------------------------------------- target-date estimate

def test_estimate_uses_history_and_atr(cfg):
    hist = TargetHistory(p25=4, median=9, p75=13, hit_rate_pct=20, trades=800, source="x")
    # Fri 20 Nov 2026 signal → entry Mon 23 Nov; ATR 10, distance 30 → fastest 3 days
    eta = estimate_target(100, 130, 10, date(2026, 11, 20), cfg, hist)
    assert eta.entry_date == date(2026, 11, 23) and eta.fastest_days == 3
    assert eta.earliest == date(2026, 11, 27)      # +4 trading days
    assert eta.likely == date(2026, 12, 4)         # +9
    assert eta.latest == date(2026, 12, 10)        # +13
    assert eta.time_stop == date(2026, 12, 21)     # +20
    assert "20% of trades" in eta.text(130)


def test_estimate_slow_stock_is_clamped_to_time_stop(cfg):
    eta = estimate_target(100, 200, 2, date(2026, 11, 20), cfg, None)   # needs 50 days at ATR pace
    assert eta.beyond_time_stop and eta.latest == eta.time_stop
    assert "may not be reached before the time stop" in eta.text(200)


def test_target_history_from_saved_backtest(tmp_path):
    folder = tmp_path / "backtest_20260101_000000"
    folder.mkdir()
    pd.DataFrame({"exit_reason": ["TARGET_HIT"] * 6 + ["STOP_HIT"] * 4,
                  "bars_held": [2, 4, 6, 8, 10, 12, 1, 1, 1, 1]}).to_csv(folder / "trades.csv", index=False)
    h = target_history(tmp_path)
    assert h.hit_rate_pct == 60 and h.median == 7 and h.trades == 10
    assert target_history(tmp_path / "empty") is None


def test_what_if_capital_changes_size_not_saved_capital(conn, setup_db):
    cfg, src, tmp = setup_db
    base = analyze_symbol(conn, cfg, "AAA", source=src, reports_dir=tmp)
    big = analyze_symbol(conn, cfg, "AAA", source=src, reports_dir=tmp, capital_override=500000)
    small = analyze_symbol(conn, cfg, "AAA", source=src, reports_dir=tmp, capital_override=5000)
    assert big.plan.qty > base.plan.qty > small.plan.qty
    assert "WHAT-IF" in big.sizing and "₹5,00,000" in big.sizing
    assert "WHAT-IF" not in base.sizing
    assert cfg.account.capital == 100000
    with pytest.raises(AnalyzeError, match="more than"):
        analyze_symbol(conn, cfg, "AAA", source=src, reports_dir=tmp, capital_override=-1)


def test_quantity_for_80_rupee_stock_and_1000_capital(cfg):
    """Manual check: ₹80 stock, stop ₹78, capital ₹1,000 (1% risk, 20% max per stock)."""
    from swing_agent.risk import position_size, sizing_steps

    # BULLISH: risk ₹10 → 10 ÷ 2 = 5 shares; one-stock cap ₹200 → 2 shares; cash → 12 shares. Qty = 2.
    assert position_size(80, 78, 1000, 1000, cfg).qty == 2
    steps = sizing_steps(80, 78, 1000, 1000, cfg)
    assert "**5 shares**" in steps[0] and "**2 shares**" in steps[1] and "**12 shares**" in steps[2]
    assert "**2**" in steps[3] and "12 shares" in steps[4]
    # CAUTION (risk × 0.5): risk ₹5 → 2 shares; still 2.
    assert position_size(80, 78, 1000, 1000, cfg, 0.5).qty == 2
    assert "× 0.5 (CAUTION market)" in sizing_steps(80, 78, 1000, 1000, cfg, 0.5, "CAUTION")[0]
    # ₹10 capital: nothing affordable within the limits.
    assert position_size(80, 78, 10, 10, cfg).qty == 0
    assert "too small" in sizing_steps(80, 78, 10, 10, cfg)[3]
