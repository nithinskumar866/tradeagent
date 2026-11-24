"""Every dashboard page renders without errors against a synthetic DB (offline)."""

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from conftest import ROOT
from fixtures.patterns import index_series, pullback_series
from fixtures.synthetic import FakeSource
from swing_agent import db
from swing_agent.backtest import run_backtest, write_outputs
from swing_agent.config import load_config
from swing_agent.events import add_event
from swing_agent.indicators import add_all_indicators
from swing_agent.market_filter import compute_regime_series
from swing_agent.scanner import setup_frame
from swing_agent.trading import log_buy, log_sell

PAGES = ["Today", "My positions", "Chart", "Backtest", "Journal", "Events", "Settings"]


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    """Synthetic DB with one saved run (a TAKE signal), an open position, a closed trade and an event."""
    from swing_agent.pipeline import daily

    path = tmp_path / "dash.db"
    monkeypatch.setenv("SWING_AGENT_DB", str(path))
    cfg = load_config()
    pb = pullback_series()
    idx = index_series("bull", len(pb))
    vix = idx.assign(open=15.0, high=15.0, low=15.0, close=15.0)
    src = FakeSource({"^NSEI": idx.reset_index(), "^INDIAVIX": vix.reset_index(), "AAA": pb.reset_index(),
                      "BBB": pullback_series(depth_steps=9, step_pct=2.0).reset_index()})
    conn = db.connect(path)
    db.upsert_symbols(conn, pd.DataFrame({"symbol": ["AAA", "BBB"], "name": ["Aaa", "Bbb"], "sector": ["IT", "Bank"]}))
    from swing_agent.data_fetcher import DataFetcher

    DataFetcher(conn, src, cfg).update()
    daily(conn, cfg, persist=True, reports_dir=tmp_path)
    last = pb.index[-1].date()
    log_buy(conn, cfg, "BBB", 900.0, 5, last - pd.Timedelta(days=30), stop=850.0)
    log_buy(conn, cfg, "AAA", 1000.0, 3, last - pd.Timedelta(days=60), stop=950.0)
    log_sell(conn, cfg, "AAA", 1050.0, 3, last - pd.Timedelta(days=50))
    add_event(conn, last, "ALL", "RBI", "policy")
    conn.close()
    return path


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(demo_db, page, tmp_path, monkeypatch):
    if page == "Backtest":  # give it a saved backtest to load
        _write_backtest(tmp_path / "reports", monkeypatch)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["page"] = page
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.header[0].value.startswith(page.split()[0])


def _write_backtest(reports, monkeypatch):
    monkeypatch.setenv("SWING_AGENT_REPORTS", str(reports))
    cfg = load_config()
    pb = pullback_series()
    idx = index_series("bull", len(pb))
    frames = {"AAA": setup_frame(add_all_indicators(pb, cfg, idx["close"]), cfg)}
    meta = pd.DataFrame({"symbol": ["AAA"], "name": ["A"], "sector": ["IT"]})
    res = run_backtest(frames, compute_regime_series(idx, None, cfg), meta, cfg)
    write_outputs(res, cfg, reports / "backtest_20200101_000000")


def test_today_log_this_buy_prefills_form(demo_db):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["page"] = "Today"
    at.run()
    assert any("TAKE (1)" in s.value for s in at.subheader)
    at.button(key=[b.key for b in at.button if (b.key or "").startswith("logbuy_")][0]).click().run()
    assert not at.exception
    assert any(t.value == "AAA" for t in at.text_input)
