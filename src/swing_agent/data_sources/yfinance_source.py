"""yfinance-backed DataSource for NSE stocks, Nifty and India VIX.

yfinance is unofficial; everything it does is kept inside this module so the source can
be swapped (e.g. for NSE bhavcopy) without touching the rest of the code.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta

import pandas as pd

from .base import DataSourceError, normalize_ohlcv

log = logging.getLogger(__name__)


def to_yf_symbol(symbol: str) -> str:
    """Map an NSE symbol to a Yahoo ticker. ``RELIANCE`` → ``RELIANCE.NS``; ``^NSEI`` unchanged."""
    if symbol.startswith("^") or "." in symbol:
        return symbol
    return f"{symbol}.NS"


class YFinanceSource:
    """Daily bars from Yahoo Finance with retries and polite throttling."""

    def __init__(
        self,
        retries: int = 3,
        backoff_seconds: float = 2.0,
        batch_size: int = 20,
        pause_seconds: float = 1.0,
    ) -> None:
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.batch_size = batch_size
        self.pause_seconds = pause_seconds

    def _download(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        import yfinance as yf  # imported lazily so tests never need it

        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return yf.download(
                    tickers=tickers,
                    start=start.isoformat(),
                    end=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
                    interval="1d",
                    auto_adjust=True,
                    group_by="ticker",
                    progress=False,
                    threads=True,
                )
            except Exception as err:  # network errors surface as many exception types
                last_err = err
                log.warning("yfinance download attempt %d failed: %s", attempt, err)
                time.sleep(self.backoff_seconds * attempt)
        raise DataSourceError(f"Download failed after {self.retries} attempts: {last_err}")

    @staticmethod
    def _split(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        if raw is None or raw.empty:
            return out
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = set(raw.columns.get_level_values(0))
            for t in tickers:
                if t in level0:
                    sub = raw[t]
                elif t in set(raw.columns.get_level_values(1)):
                    sub = raw.xs(t, axis=1, level=1)
                else:
                    continue
                df = normalize_ohlcv(sub.dropna(how="all"))
                if not df.empty:
                    out[t] = df
        elif len(tickers) == 1:
            df = normalize_ohlcv(raw.dropna(how="all"))
            if not df.empty:
                out[tickers[0]] = df
        return out

    def fetch_daily_batch(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """Fetch many symbols in throttled chunks. Keys are the original (non-Yahoo) symbols."""
        result: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), self.batch_size):
            chunk = symbols[i : i + self.batch_size]
            mapping = {to_yf_symbol(s): s for s in chunk}
            tickers = list(mapping)
            raw = self._download(tickers, start, end)
            for t, df in self._split(raw, tickers).items():
                result[mapping[t]] = df
            if i + self.batch_size < len(symbols):
                time.sleep(self.pause_seconds)
        return result

    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Fetch one symbol. Raises DataSourceError if nothing comes back."""
        data = self.fetch_daily_batch([symbol], start, end)
        if symbol not in data:
            raise DataSourceError(f"No data returned for {symbol}")
        return data[symbol]
