"""Synthetic OHLCV generators and a fake DataSource for offline tests."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from swing_agent.data_sources import DataSourceError


def make_ohlcv(
    n: int = 300,
    start: str = "2020-01-01",
    start_price: float = 100.0,
    drift: float = 0.0005,
    vol: float = 0.01,
    seed: int = 0,
    base_volume: int = 1_000_000,
) -> pd.DataFrame:
    """Random-walk business-day OHLCV with a ``date`` column (valid: high ≥ max(open, close) etc.)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start=start, periods=n)
    rets = rng.normal(drift, vol, n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]]) * (1 + rng.normal(0, vol / 4, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    volume = (base_volume * (1 + rng.uniform(-0.3, 0.3, n))).astype(int)
    return pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )


class FakeSource:
    """In-memory DataSource. ``data[symbol]`` is the full 'true' history the source knows."""

    def __init__(self, data: dict[str, pd.DataFrame], fail: set[str] | None = None,
                 offline: bool = False) -> None:
        self.data = data
        self.fail = fail or set()
        self.offline = offline
        self.calls: list[tuple[tuple[str, ...], date, date]] = []

    def _slice(self, sym: str, start: date, end: date) -> pd.DataFrame | None:
        df = self.data.get(sym)
        if df is None or sym in self.fail:
            return None
        mask = (df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))
        return df[mask].reset_index(drop=True)

    def fetch_daily_batch(self, symbols, start, end):
        if self.offline:
            raise DataSourceError("network unreachable")
        self.calls.append((tuple(symbols), start, end))
        out = {}
        for s in symbols:
            d = self._slice(s, start, end)
            if d is not None and not d.empty:
                out[s] = d
        return out

    def fetch_daily(self, symbol, start, end):
        res = self.fetch_daily_batch([symbol], start, end)
        if symbol not in res:
            raise DataSourceError(f"No data returned for {symbol}")
        return res[symbol]
