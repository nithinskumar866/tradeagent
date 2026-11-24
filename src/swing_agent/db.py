"""SQLite schema, connections and read/write helpers for ``data/market.db``."""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

import pandas as pd

from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Override with the SWING_AGENT_DB environment variable (e.g. to experiment on a copy).
DEFAULT_DB_PATH: Path = Path(os.environ.get("SWING_AGENT_DB") or PROJECT_ROOT / "data" / "market.db")

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
    path = Path(db_path) if db_path else Path(os.environ.get("SWING_AGENT_DB") or DEFAULT_DB_PATH)
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


# Columns added on top of the spec schema (kept as additive migrations):
#   trades.stop_initial: the stop at entry, so R stays fixed after the stop is moved
#   signals.status / status_note: TAKE or WATCH and why
#   runs.regime: lets the next run detect a regime change
MIGRATIONS = [
    ("trades", "stop_initial", "REAL"),
    ("signals", "status", "TEXT"),
    ("signals", "status_note", "TEXT"),
    ("runs", "regime", "TEXT"),
]


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't exist, then apply additive migrations."""
    conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
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


# ---------------------------------------------------------------- trades

def insert_trade(
    conn: sqlite3.Connection,
    symbol: str,
    side: str,
    trade_date: date | str,
    price: float,
    qty: int,
    fees: float,
    stop: float | None = None,
    target: float | None = None,
    signal_id: int | None = None,
    note: str | None = None,
) -> int:
    """Insert one BUY/SELL row and return its id. For BUYs, ``stop_initial`` = ``stop``."""
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO trades(symbol, side, date, price, qty, stop, target, fees, signal_id, note, stop_initial) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, side, str(trade_date), price, qty, stop, target, fees, signal_id, note,
             stop if side == "BUY" else None),
        )
    return int(cur.lastrowid)


def get_trades(conn: sqlite3.Connection) -> pd.DataFrame:
    """All trades in entry order (date, then id)."""
    return pd.read_sql_query("SELECT * FROM trades ORDER BY date, id", conn)


def delete_trade(conn: sqlite3.Connection, trade_id: int) -> bool:
    """Delete a trade row (to fix a mistake). Returns True if a row was deleted."""
    with transaction(conn):
        cur = conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
    return cur.rowcount > 0


def update_stop(conn: sqlite3.Connection, trade_ids: list[int], new_stop: float) -> None:
    """Set the current stop on the given BUY rows (``stop_initial`` is left unchanged)."""
    with transaction(conn):
        conn.executemany("UPDATE trades SET stop = ? WHERE id = ? AND side = 'BUY'",
                         [(new_stop, i) for i in trade_ids])


# ---------------------------------------------------------------- runs, signals, alerts

def latest_signal(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    """Most recent stored signal for a symbol."""
    return conn.execute(
        "SELECT * FROM signals WHERE symbol = ? ORDER BY run_date DESC, id DESC LIMIT 1", (symbol,)
    ).fetchone()


def previous_run_regime(conn: sqlite3.Connection, before: str) -> str | None:
    """Regime recorded by the latest successful run strictly before ``before`` (YYYY-MM-DD)."""
    row = conn.execute(
        "SELECT regime FROM runs WHERE run_date < ? AND regime IS NOT NULL AND status = 'OK' "
        "ORDER BY run_date DESC LIMIT 1", (before,)
    ).fetchone()
    return row[0] if row else None


def start_run(conn: sqlite3.Connection, run_date: str, started_at: str) -> None:
    """Create or reset the runs row for ``run_date``."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs(run_date, started_at, status) VALUES (?, ?, 'RUNNING') "
            "ON CONFLICT(run_date) DO UPDATE SET started_at=excluded.started_at, status='RUNNING', "
            "finished_at=NULL, notes=NULL",
            (run_date, started_at),
        )


def finish_run(conn: sqlite3.Connection, run_date: str, finished_at: str, status: str,
               data_last_date: str | None, regime: str | None, notes: str | None) -> None:
    """Record the outcome of a run."""
    with transaction(conn):
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, data_last_date=?, regime=?, notes=? WHERE run_date=?",
            (finished_at, status, data_last_date, regime, notes, run_date),
        )


def replace_run_outputs(conn: sqlite3.Connection, run_date: str,
                        signals: list[dict], alerts: list[dict]) -> list[int]:
    """Idempotently replace the signals and alerts of ``run_date``. Returns new signal ids."""
    ids: list[int] = []
    with transaction(conn):
        conn.execute("DELETE FROM signals WHERE run_date = ?", (run_date,))
        conn.execute("DELETE FROM alerts WHERE run_date = ?", (run_date,))
        for s in signals:
            cols = ["run_date", *s.keys()]
            cur = conn.execute(
                f"INSERT INTO signals({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [run_date, *s.values()],
            )
            ids.append(int(cur.lastrowid))
        conn.executemany(
            "INSERT INTO alerts(run_date, symbol, type, message, suggested_action) VALUES (?, ?, ?, ?, ?)",
            [(run_date, a["symbol"], a["type"], a["message"], a["suggested_action"]) for a in alerts],
        )
    return ids
