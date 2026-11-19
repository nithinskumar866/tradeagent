"""DataSource interface and shared OHLCV normalisation."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd

OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class DataSourceError(Exception):
    """Raised when a source cannot deliver data (network down, symbol unknown, ...)."""


@runtime_checkable
class DataSource(Protocol):
    """A provider of daily OHLCV bars."""

    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Return columns date, open, high, low, close, volume (date ascending), inclusive of end."""
        ...

    def fetch_daily_batch(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """Fetch many symbols at once. Missing/failed symbols are simply absent from the result."""
        ...


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw frame (date index or column, any column case) to the standard OHLCV layout.

    Drops rows without a close, de-duplicates dates, sorts ascending and strips timezones.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    out = df.copy()
    out.columns = [str(c).strip().lower().replace(" ", "_") for c in out.columns]
    if "date" not in out.columns:
        out = out.reset_index()
        first = out.columns[0]
        out = out.rename(columns={first: "date"})
        out.columns = [str(c).strip().lower() for c in out.columns]
    missing = [c for c in OHLCV_COLUMNS if c not in out.columns]
    if missing:
        raise DataSourceError(f"Data is missing columns: {missing}")
    dates = pd.to_datetime(out["date"])
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    out["date"] = dates.dt.normalize()
    out = out[OHLCV_COLUMNS].dropna(subset=["close"])
    out["volume"] = out["volume"].fillna(0)
    out = out.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)
    return out
