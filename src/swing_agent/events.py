"""Event calendar: lookups used by the scanner and exit manager.

(CSV sync, CLI management and yfinance earnings dates arrive in milestone 6.)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

EVENT_TYPES = ("RESULTS", "RBI", "BUDGET", "FED", "ELECTION", "OTHER")


def get_events(
    conn: sqlite3.Connection,
    start: date | None = None,
    end: date | None = None,
    types: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Events between start and end (inclusive) as a DataFrame sorted by date."""
    sql = "SELECT id, date, symbol, type, note, source FROM events WHERE 1=1"
    params: list = []
    if start:
        sql += " AND date >= ?"
        params.append(str(start))
    if end:
        sql += " AND date <= ?"
        params.append(str(end))
    if types:
        sql += f" AND type IN ({','.join('?' * len(types))})"
        params.extend(types)
    return pd.read_sql_query(sql + " ORDER BY date, symbol", conn, params=params)


def results_dates_by_symbol(conn: sqlite3.Connection) -> dict[str, list[date]]:
    """All known RESULTS dates per symbol (ascending)."""
    df = get_events(conn, types=("RESULTS",))
    out: dict[str, list[date]] = {}
    for sym, d in zip(df["symbol"], df["date"]):
        out.setdefault(sym, []).append(date.fromisoformat(d))
    return {k: sorted(set(v)) for k, v in out.items()}


@dataclass
class ResultsCheck:
    """Outcome of a 'results soon' lookup for one symbol."""

    next_date: date | None
    trading_days_away: int | None

    @property
    def unknown(self) -> bool:
        """True if no upcoming results date is known."""
        return self.next_date is None

    def within(self, days: int) -> bool:
        """True if results fall within ``days`` trading days (today counts as 0)."""
        return self.trading_days_away is not None and self.trading_days_away <= days


def check_results(dates: list[date] | None, asof: date) -> ResultsCheck:
    """Find the next results date on/after ``asof`` from a sorted list.

    Distance is counted in Mon–Fri days (no holiday calendar), so an exchange holiday
    in between is counted as a trading day; around holidays the count can be one high.
    """
    for d in dates or []:
        if d >= asof:
            return ResultsCheck(d, max(0, int(np.busday_count(asof, d))))
    return ResultsCheck(None, None)


# ---------------------------------------------------------------- editing

class EventError(Exception):
    """Raised for an invalid event entry."""


def _clean_event(d, symbol: str, etype: str) -> tuple[str, str, str]:
    try:
        day = pd.Timestamp(d).date().isoformat()
    except Exception:
        raise EventError(f"Bad event date {d!r}; use YYYY-MM-DD.") from None
    sym = str(symbol).strip().upper() or "ALL"
    t = str(etype).strip().upper()
    if t not in EVENT_TYPES:
        raise EventError(f"Event type must be one of {', '.join(EVENT_TYPES)} (got {etype!r}).")
    return day, sym, t


def add_event(conn: sqlite3.Connection, d, symbol: str, etype: str, note: str = "",
              source: str = "manual") -> int | None:
    """Add one event; returns its id, or None if the same (date, symbol, type) already exists."""
    day, sym, t = _clean_event(d, symbol, etype)
    exists = conn.execute("SELECT id FROM events WHERE date=? AND symbol=? AND type=?", (day, sym, t)).fetchone()
    if exists:
        return None
    cur = conn.execute("INSERT INTO events(date, symbol, type, note, source) VALUES (?, ?, ?, ?, ?)",
                       (day, sym, t, note or "", source))
    conn.commit()
    return int(cur.lastrowid)


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Delete an event by id."""
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    return cur.rowcount > 0


def import_events(conn: sqlite3.Connection, df: pd.DataFrame, source: str = "manual") -> tuple[int, list[str]]:
    """Import rows with columns date,symbol,type[,note]. Returns (added, problems)."""
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    missing = [c for c in ("date", "symbol", "type") if c not in df.columns]
    if missing:
        raise EventError(f"Events CSV is missing columns: {missing}. Expected date,symbol,type,note.")
    added, problems = 0, []
    for i, r in enumerate(df.itertuples(index=False), start=2):
        try:
            note = getattr(r, "note", "")
            note = "" if note is None or (isinstance(note, float) and pd.isna(note)) else str(note)
            if add_event(conn, r.date, r.symbol, r.type, note, source) is not None:
                added += 1
        except EventError as err:
            problems.append(f"row {i}: {err}")
    return added, problems
