"""Orchestrates the daily scan: load prices → indicators → regime → signals → ranking."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date

import pandas as pd

from . import db
from .config import Config
from .events import results_dates_by_symbol
from .indicators import add_all_indicators
from .market_filter import compute_regime_series, regime_at
from .scanner import ScanResult, rank_and_select, scan, setup_frame

log = logging.getLogger(__name__)


class NoDataError(Exception):
    """Raised when the DB has no Nifty data yet (run `swing.py init`)."""


@dataclass
class MarketData:
    """Everything the strategy needs, computed from the DB."""

    frames: dict[str, pd.DataFrame]   # symbol → setup_frame (indicators + conditions)
    regime_series: pd.DataFrame
    meta: pd.DataFrame                # symbol, name, sector
    latest_trading_day: date


def load_market_data(
    conn: sqlite3.Connection, cfg: Config, symbols: list[str] | None = None
) -> MarketData:
    """Read prices from the DB and compute indicators, setup conditions and regime."""
    index_df = db.get_prices(conn, cfg.data.index_symbol)
    if index_df.empty:
        raise NoDataError("No Nifty data in the database. Run `python swing.py init` first.")
    vix_df = db.get_prices(conn, cfg.data.vix_symbol)
    meta = db.get_symbols(conn)
    if symbols is not None:
        meta = meta[meta["symbol"].isin(symbols)]

    frames: dict[str, pd.DataFrame] = {}
    for sym in meta["symbol"]:
        px = db.get_prices(conn, sym)
        if px.empty:
            continue
        frames[sym] = setup_frame(add_all_indicators(px, cfg, index_df["close"]), cfg)
    return MarketData(
        frames=frames,
        regime_series=compute_regime_series(index_df, vix_df, cfg),
        meta=meta.reset_index(drop=True),
        latest_trading_day=index_df.index[-1].date(),
    )


def run_scan(
    conn: sqlite3.Connection,
    cfg: Config,
    asof: date | None = None,
    market: MarketData | None = None,
    held: set[str] | None = None,
    open_sector_counts: dict[str, int] | None = None,
) -> tuple[ScanResult, str | None]:
    """Scan on ``asof`` (default: latest trading day). Returns (result, previous-day regime)."""
    market = market or load_market_data(conn, cfg)
    asof = asof or market.latest_trading_day
    first_day = market.regime_series.index[0].date()
    if asof < first_day:
        raise NoDataError(f"No data on or before {asof}; stored history starts on {first_day}.")
    regime = regime_at(market.regime_series, asof, cfg)
    prev = market.regime_series.loc[: pd.Timestamp(asof)]["regime"]
    prev_regime = str(prev.iloc[-2]) if len(prev) >= 2 else None

    result = scan(
        market.frames, regime.date, regime, cfg, market.meta,
        results_lookup=results_dates_by_symbol(conn),
    )
    result.signals = rank_and_select(
        result.signals,
        max_positions=regime.max_positions,
        held=held,
        open_sector_counts=open_sector_counts,
        max_per_sector=cfg.account.max_per_sector,
    )
    return result, prev_regime
