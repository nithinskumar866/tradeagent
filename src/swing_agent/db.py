"""SQLite schema, connections and read/write helpers for ``data/market.db``."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

DEFAULT_DB_PATH: Path = PROJECT_ROOT / "data" / "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
  symbol TEXT PRIMARY KEY, name TEXT, sector TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
  PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, date TEXT, symbol TEXT,
  type TEXT,
  note TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, run_date TEXT, symbol TEXT, setup TEXT,
  signal_close REAL, buy_zone_low REAL, buy_zone_high REAL,
  stop REAL, target REAL, qty INTEGER, risk_amount REAL, rr REAL,
  score REAL, reasons_json TEXT, warnings_json TEXT, regime TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, symbol TEXT, side TEXT CHECK (side IN ('BUY','SELL')),
  date TEXT, price REAL, qty INTEGER, stop REAL, target REAL,
  fees REAL, signal_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, run_date TEXT, symbol TEXT,
  type TEXT,
  message TEXT, suggested_action TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  status TEXT, data_last_date TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_date);
CREATE INDEX IF NOT EXISTS idx_alerts_run ON alerts(run_date);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date, symbol);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, date);
"""

PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]


class DatabaseError(Exception):
    """Friendly wrapper for database problems (e.g. the DB file is locked)."""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection (creating the folder if needed) and ensure the schema exists."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        init_schema(conn)
    except sqlite3.OperationalError as err:
        raise _friendly(err) from None
    return conn


def _friendly(err: sqlite3.OperationalError) -> DatabaseError:
    if "locked" in str(err).lower():
        return DatabaseError(
            "The database is locked by another program (maybe the dashboard or another run). "
            "Close it and try again."
        )
    return DatabaseError(f"Database error: {err}")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on error, and translate 'database is locked'."""
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as err:
        conn.rollback()
        raise _friendly(err) from None
    except Exception:
        conn.rollback()
        raise


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't exist."""
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- symbols

def upsert_symbols(conn: sqlite3.Connection, watchlist: pd.DataFrame) -> None:
    """Insert/update watchlist symbols as active; mark symbols missing from the list inactive."""
    rows = [(r.symbol, r.name, r.sector) for r in watchlist.itertuples(index=False)]
    with transaction(conn):
        conn.execute("UPDATE symbols SET active = 0")
        conn.executemany(
            "INSERT INTO symbols(symbol, name, sector, active) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(symbol) DO UPDATE SET name=excluded.name, sector=excluded.sector, active=1",
            rows,
        )


def get_symbols(conn: sqlite3.Connection, active_only: bool = True) -> pd.DataFrame:
    """Return the symbols table as a DataFrame (symbol, name, sector, active)."""
    sql = "SELECT symbol, name, sector, active FROM symbols"
    if active_only:
        sql += " WHERE active = 1"
    return pd.read_sql_query(sql + " ORDER BY symbol", conn)


# ---------------------------------------------------------------- prices

def _to_rows(symbol: str, df: pd.DataFrame) -> list[tuple]:
    dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    vol = df["volume"].fillna(0).astype("int64")
    return list(
        zip(
            [symbol] * len(df),
            dates,
            df["open"].astype(float),
            df["high"].astype(float),
            df["low"].astype(float),
            df["close"].astype(float),
            vol.astype(int),
        )
    )


def upsert_prices(conn: sqlite3.Connection, symbol: str, df: pd.DataFrame) -> int:
    """Insert or replace OHLCV rows for one symbol. ``df`` needs a ``date`` column. Returns row count."""
    if df.empty:
        return 0
    rows = _to_rows(symbol, df)
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO prices(symbol, date, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def replace_prices(conn: sqlite3.Connection, symbol: str, df: pd.DataFrame) -> int:
    """Delete all stored prices for ``symbol`` and insert ``df`` in one transaction."""
    rows = _to_rows(symbol, df) if not df.empty else []
    with transaction(conn):
        conn.execute("DELETE FROM prices WHERE symbol = ?", (symbol,))
        conn.executemany(
            "INSERT INTO prices(symbol, date, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def get_prices(
    conn: sqlite3.Connection,
    symbol: str,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    """Return OHLCV for a symbol indexed by a DatetimeIndex named ``date`` (ascending)."""
    sql = "SELECT date, open, high, low, close, volume FROM prices WHERE symbol = ?"
    params: list = [symbol]
    if start is not None:
        sql += " AND date >= ?"
        params.append(str(start))
    if end is not None:
        sql += " AND date <= ?"
        params.append(str(end))
    df = pd.read_sql_query(sql + " ORDER BY date", conn, params=params)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def last_price_date(conn: sqlite3.Connection, symbol: str) -> date | None:
    """Latest stored bar date for a symbol, or None."""
    row = conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    return date.fromisoformat(row[0]) if row and row[0] else None


def nth_last_price_date(conn: sqlite3.Connection, symbol: str, n: int) -> date | None:
    """Date of the n-th most recent stored bar (n=1 is the latest), or the earliest if fewer exist."""
    rows = conn.execute(
        "SELECT date FROM prices WHERE symbol = ? ORDER BY date DESC LIMIT ?", (symbol, n)
    ).fetchall()
    return date.fromisoformat(rows[-1][0]) if rows else None


def price_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """Per-symbol bar count and first/last date."""
    return pd.read_sql_query(
        "SELECT symbol, COUNT(*) AS bars, MIN(date) AS first_date, MAX(date) AS last_date "
        "FROM prices GROUP BY symbol ORDER BY symbol",
        conn,
    )
