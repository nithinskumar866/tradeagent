import pandas as pd
import pytest

from conftest import ROOT
from fixtures.synthetic import make_ohlcv
from swing_agent import db
from swing_agent.watchlist import (
    WatchlistError,
    clean_watchlist,
    load_watchlist,
    parse_niftyindices_csv,
)

TABLES = {"symbols", "prices", "events", "signals", "trades", "alerts", "runs"}


def test_schema_created(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_trades_side_check(conn):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO trades(symbol, side, date, price, qty) VALUES ('X','HOLD','2024-01-01',1,1)")


def test_price_upsert_is_idempotent_and_roundtrips(conn):
    df = make_ohlcv(30)
    db.upsert_prices(conn, "AAA", df)
    db.upsert_prices(conn, "AAA", df)
    out = db.get_prices(conn, "AAA")
    assert len(out) == 30
    assert out.index[0] == df["date"].iloc[0]
    assert out["close"].iloc[-1] == pytest.approx(df["close"].iloc[-1])
    assert db.last_price_date(conn, "AAA") == df["date"].iloc[-1].date()
    assert db.nth_last_price_date(conn, "AAA", 5) == df["date"].iloc[-5].date()


def test_replace_prices(conn):
    db.upsert_prices(conn, "AAA", make_ohlcv(30))
    db.replace_prices(conn, "AAA", make_ohlcv(10, start="2021-01-01"))
    assert len(db.get_prices(conn, "AAA")) == 10


def test_symbols_sync_deactivates_removed(conn):
    wl = clean_watchlist(pd.DataFrame({"symbol": ["a", "b"], "name": ["A", "B"], "sector": ["X", "Y"]}))
    db.upsert_symbols(conn, wl)
    db.upsert_symbols(conn, wl.iloc[:1])
    assert db.get_symbols(conn)["symbol"].tolist() == ["A"]
    assert len(db.get_symbols(conn, active_only=False)) == 2


def test_shipped_watchlist_loads():
    wl = load_watchlist(ROOT / "data/watchlists/nifty100.csv")
    assert len(wl) >= 90
    assert wl["symbol"].is_unique
    assert {"RELIANCE", "HDFCBANK", "M&M", "BAJAJ-AUTO"} <= set(wl["symbol"])


def test_watchlist_cleaning():
    df = pd.DataFrame({"Symbol": [" infy ", "INFY", ""], "Name": ["Infosys", "dup", "x"], "Sector": ["IT", "IT", ""]})
    out = clean_watchlist(df)
    assert out["symbol"].tolist() == ["INFY"]


def test_empty_watchlist_errors(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("symbol,name,sector\n", encoding="utf-8")
    with pytest.raises(WatchlistError, match="empty"):
        load_watchlist(p)


def test_watchlist_missing_columns(tmp_path):
    p = tmp_path / "w.csv"
    p.write_text("ticker\nINFY\n", encoding="utf-8")
    with pytest.raises(WatchlistError, match="missing columns"):
        load_watchlist(p)


def test_parse_niftyindices_format():
    content = (
        b"Company Name,Industry,Symbol,Series,ISIN Code\n"
        b"Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
        b"Reliance Industries Ltd.,\"Oil, Gas & Consumable Fuels\",RELIANCE,EQ,INE002A01018\n"
    )
    df = parse_niftyindices_csv(content)
    assert df.to_dict("records")[1] == {
        "symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "sector": "Oil, Gas & Consumable Fuels"}
