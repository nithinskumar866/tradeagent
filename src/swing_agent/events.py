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
