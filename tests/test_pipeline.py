import pandas as pd

from fixtures.patterns import index_series, pullback_series
from fixtures.synthetic import FakeSource
from swing_agent import db
from swing_agent.pipeline import daily
from swing_agent.trading import log_buy


def _source():
    pb = pullback_series()
    n = len(pb)
    idx = index_series("bull", n)
    vix = idx.assign(open=15.0, high=15.0, low=15.0, close=15.0)
    other = pullback_series(depth_steps=9, step_pct=2.0)  # no setup
    to_df = lambda f: f.reset_index()  # FakeSource serves frames with a date column
    return FakeSource({"^NSEI": to_df(idx), "^INDIAVIX": to_df(vix), "AAA": to_df(pb), "BBB": to_df(other)})


def _cfg(cfg, tmp_path):
    wl = tmp_path / "wl.csv"
    wl.write_text("symbol,name,sector\nAAA,Aaa Ltd,IT\nBBB,Bbb Ltd,Bank\n", encoding="utf-8")
    c = cfg.model_copy(deep=True)
    c.universe.watchlist_file = str(wl)
    return c


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("signals", "alerts", "runs")}


def test_run_twice_same_day_does_not_duplicate(conn, cfg, tmp_path):
    c = _cfg(cfg, tmp_path)
    src = _source()
    first = daily(conn, c, do_update=True, persist=True, source=src, reports_dir=tmp_path / "reports")
    assert [s.symbol for s in first.scan.signals] == ["AAA"]
    assert first.scan.signals[0].status == "TAKE" and first.scan.signals[0].signal_id

    # Hold BBB with a stop above its latest low so an exit alert is produced.
    bbb_low = db.get_prices(conn, "BBB", end=first.run_date)["low"].iloc[-1]
    log_buy(conn, c, "BBB", bbb_low * 1.2, 5, first.run_date - pd.Timedelta(days=10), stop=bbb_low * 1.05)

    second = daily(conn, c, do_update=True, persist=True, source=src, reports_dir=tmp_path / "reports")
    after_second = _counts(conn)
    third = daily(conn, c, do_update=True, persist=True, source=src, reports_dir=tmp_path / "reports")
    assert _counts(conn) == after_second
    assert after_second["runs"] == 1 and after_second["signals"] == 1
    assert any(a.type == "STOP_HIT" and a.symbol == "BBB" for a in third.alerts)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "OK"

    text = third.report_path.read_text(encoding="utf-8")
    assert "STOP_HIT" in text and "AAA" in text and "Not investment advice" in text
    assert third.report_path.name == f"{third.run_date}.md"


def test_scan_writes_nothing(conn, cfg, tmp_path):
    c = _cfg(cfg, tmp_path)
    daily(conn, c, do_update=True, persist=False, source=_source())
    assert _counts(conn) == {"signals": 0, "alerts": 0, "runs": 0}


def test_regime_change_alert_against_previous_run(conn, cfg, tmp_path):
    c = _cfg(cfg, tmp_path)
    daily(conn, c, do_update=True, persist=True, source=_source(), reports_dir=tmp_path)
    rd = conn.execute("SELECT run_date FROM runs").fetchone()[0]
    conn.execute("INSERT INTO runs(run_date, status, regime) VALUES ('2000-01-03', 'OK', 'BEARISH')")
    conn.execute("UPDATE runs SET regime = NULL WHERE run_date = ?", (rd,))
    conn.commit()
    out = daily(conn, c, persist=True, reports_dir=tmp_path)
    assert any(a.type == "REGIME_CHANGE" and "BEARISH to BULLISH" in a.message for a in out.alerts)
