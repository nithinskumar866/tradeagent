from datetime import datetime, timedelta

import pandas as pd
import pytest

from fixtures.synthetic import FakeSource, make_ohlcv
from swing_agent import db
from swing_agent.data_fetcher import IST, DataFetcher, check_quality, drop_phantom_bars
from swing_agent.data_sources import normalize_ohlcv
from swing_agent.data_sources.yfinance_source import YFinanceSource, to_yf_symbol
from swing_agent.watchlist import clean_watchlist

N = 400


def _universe():
    data = {
        "^NSEI": make_ohlcv(N, seed=1, start_price=15000),
        "^INDIAVIX": make_ohlcv(N, seed=2, start_price=15),
        "AAA": make_ohlcv(N, seed=3),
        "BBB": make_ohlcv(N, seed=4),
    }
    return data


def _setup(conn, cfg, data, **kw):
    wl = clean_watchlist(pd.DataFrame({"symbol": ["AAA", "BBB"], "name": ["A", "B"], "sector": ["X", "Y"]}))
    db.upsert_symbols(conn, wl)
    src = FakeSource(data, **kw)
    return DataFetcher(conn, src, cfg), src


def _now_after(df_date) -> datetime:
    """An IST 'now' at 18:30 on the given date."""
    d = pd.Timestamp(df_date).date()
    return datetime(d.year, d.month, d.day, 18, 30, tzinfo=IST)


def test_initial_download_stores_full_history(conn, cfg):
    data = _universe()
    fetcher, src = _setup(conn, cfg, data)
    rep = fetcher.update(now=_now_after(data["AAA"]["date"].iloc[-1]))
    assert set(rep.full_downloads) == set(data)
    for sym in data:
        assert len(db.get_prices(conn, sym)) == N
    assert rep.latest_trading_day == data["^NSEI"]["date"].iloc[-1].date()
    assert not rep.failed and not rep.offline


def test_incremental_update_adds_only_new_bars(conn, cfg):
    full = _universe()
    truncated = {s: df.iloc[: N - 7].reset_index(drop=True) for s, df in full.items()}
    fetcher, src = _setup(conn, cfg, truncated)
    fetcher.update(now=_now_after(truncated["AAA"]["date"].iloc[-1]))

    src.data = full
    src.calls.clear()
    rep = fetcher.update(now=_now_after(full["AAA"]["date"].iloc[-1]))
    assert rep.new_bars == {s: 7 for s in full}
    assert not rep.readjusted
    # fetched from the overlap window, not from the start of history
    (_, start, _), = src.calls
    assert pd.Timestamp(start) == full["AAA"]["date"].iloc[N - 7 - cfg.data.refetch_overlap_days]
    for sym in full:
        assert len(db.get_prices(conn, sym)) == N


def test_rerun_same_day_adds_nothing(conn, cfg):
    data = _universe()
    fetcher, _ = _setup(conn, cfg, data)
    now = _now_after(data["AAA"]["date"].iloc[-1])
    fetcher.update(now=now)
    rep = fetcher.update(now=now)
    assert rep.total_new_bars == 0
    assert len(db.get_prices(conn, "AAA")) == N


def test_corporate_action_triggers_full_redownload(conn, cfg):
    full = _universe()
    truncated = {s: df.iloc[: N - 3].reset_index(drop=True) for s, df in full.items()}
    fetcher, src = _setup(conn, cfg, truncated)
    fetcher.update(now=_now_after(truncated["AAA"]["date"].iloc[-1]))

    adjusted = full["AAA"].copy()
    for c in ["open", "high", "low", "close"]:
        adjusted[c] = adjusted[c] / 2  # 1:1 bonus re-adjusts the whole history
    src.data = {**full, "AAA": adjusted}
    rep = fetcher.update(now=_now_after(full["AAA"]["date"].iloc[-1]))
    assert rep.readjusted == ["AAA"]
    stored = db.get_prices(conn, "AAA")
    assert len(stored) == N
    assert stored["close"].iloc[0] == pytest.approx(adjusted["close"].iloc[0])
    assert rep.new_bars["AAA"] == 3


def test_failed_symbol_is_skipped_and_listed(conn, cfg):
    data = _universe()
    fetcher, _ = _setup(conn, cfg, data, fail={"BBB"})
    rep = fetcher.update(now=_now_after(data["AAA"]["date"].iloc[-1]))
    assert "BBB" in rep.failed
    assert any(i.kind == "FAILED" and i.symbol == "BBB" for i in rep.issues)
    assert not rep.offline


def test_offline_keeps_cache_and_flags(conn, cfg):
    data = _universe()
    fetcher, src = _setup(conn, cfg, data)
    now = _now_after(data["AAA"]["date"].iloc[-1])
    fetcher.update(now=now)
    src.offline = True
    rep = fetcher.update(now=now + timedelta(days=1))
    assert rep.offline
    assert len(db.get_prices(conn, "AAA")) == N


def test_incomplete_today_bar_dropped_during_market_hours(conn, cfg):
    data = _universe()
    fetcher, _ = _setup(conn, cfg, data)
    last = data["AAA"]["date"].iloc[-1].date()
    fetcher.update(now=datetime(last.year, last.month, last.day, 11, 0, tzinfo=IST))
    assert db.last_price_date(conn, "AAA") == data["AAA"]["date"].iloc[-2].date()


def test_quality_checks(conn):
    idx = make_ohlcv(50, seed=1)
    db.upsert_prices(conn, "^NSEI", idx)

    stale = make_ohlcv(45, seed=2)
    db.upsert_prices(conn, "STALE", stale)

    bad = make_ohlcv(50, seed=3)
    bad.loc[10, "volume"] = 0
    bad.loc[20, "high"] = bad.loc[20, "low"] * 0.9
    bad.loc[30, "close"] = -1
    bad = bad.drop(index=range(35, 42))  # 7 business days missing → 9 calendar-day gap
    db.upsert_prices(conn, "BAD", bad)

    issues = check_quality(conn, ["^NSEI", "STALE", "BAD"], "^NSEI", no_volume_symbols={"^NSEI"})
    kinds = {(i.symbol, i.kind) for i in issues}
    assert ("STALE", "STALE") in kinds
    assert {("BAD", "ZERO_VOLUME"), ("BAD", "HIGH_LT_LOW"), ("BAD", "BAD_PRICE"), ("BAD", "GAP")} <= kinds
    assert not any(s == "^NSEI" for s, _ in kinds)


def test_quality_since_limits_window(conn):
    df = make_ohlcv(100, seed=1)
    df.loc[5, "volume"] = 0
    db.upsert_prices(conn, "^NSEI", df)
    db.upsert_prices(conn, "OLD", df)
    since = df["date"].iloc[50].date()
    assert check_quality(conn, ["OLD"], "^NSEI", since=since) == []
    assert check_quality(conn, ["OLD"], "^NSEI")[0].kind == "ZERO_VOLUME"


# ---------------------------------------------------------------- yfinance adapter (offline)

def test_symbol_mapping():
    assert to_yf_symbol("RELIANCE") == "RELIANCE.NS"
    assert to_yf_symbol("M&M") == "M&M.NS"
    assert to_yf_symbol("^NSEI") == "^NSEI"
    assert to_yf_symbol("^INDIAVIX") == "^INDIAVIX"


def test_split_multiindex_frame():
    dates = pd.date_range("2024-01-01", periods=3, tz="Asia/Kolkata", name="Date")
    fields = ["Open", "High", "Low", "Close", "Volume"]
    cols = pd.MultiIndex.from_product([["AAA.NS", "BBB.NS"], fields])
    raw = pd.DataFrame(1.0, index=dates, columns=cols)
    raw[("BBB.NS", "Close")] = float("nan")  # BBB returned nothing usable
    out = YFinanceSource._split(raw, ["AAA.NS", "BBB.NS"])
    assert list(out) == ["AAA.NS"]
    assert list(out["AAA.NS"].columns) == ["date", "open", "high", "low", "close", "volume"]
    assert out["AAA.NS"]["date"].dt.tz is None


def test_normalize_drops_dupes_and_sorts():
    raw = pd.DataFrame(
        {"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3], "Close": [1, 2, 3], "Volume": [1, None, 3]},
        index=pd.to_datetime(["2024-01-03", "2024-01-02", "2024-01-03"]),
    )
    out = normalize_ohlcv(raw)
    assert out["date"].tolist() == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
    assert out["close"].tolist() == [2, 3]
    assert out["volume"].tolist() == [0, 3]


def test_phantom_bars_dropped_for_stocks_not_index(conn, cfg):
    data = _universe()
    for sym in ["AAA", "^NSEI"]:
        df = data[sym]
        df.loc[100, ["open", "high", "low", "close"]] = df.loc[99, "close"]
        df.loc[100, "volume"] = 0
    fetcher, _ = _setup(conn, cfg, data)
    fetcher.update(now=_now_after(data["AAA"]["date"].iloc[-1]))
    assert len(db.get_prices(conn, "AAA")) == N - 1
    assert len(db.get_prices(conn, "^NSEI")) == N


def test_drop_phantom_keeps_real_zero_volume_bar():
    df = make_ohlcv(5)
    df.loc[2, "volume"] = 0  # zero volume but a real price range: keep (quality check flags it)
    assert len(drop_phantom_bars(df)) == 5
