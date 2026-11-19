"""Pluggable market-data sources."""

from __future__ import annotations

from .base import DataSource, DataSourceError, normalize_ohlcv


def get_source(name: str) -> DataSource:
    """Factory: return a DataSource implementation by config name."""
    if name == "yfinance":
        from .yfinance_source import YFinanceSource

        return YFinanceSource()
    raise ValueError(f"Unknown data source: {name}")


__all__ = ["DataSource", "DataSourceError", "normalize_ohlcv", "get_source"]
