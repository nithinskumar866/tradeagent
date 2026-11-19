"""Market regime from Nifty trend and India VIX.

| BULLISH | close > EMA50 and EMA50 > EMA200                    | normal risk          |
| CAUTION | close > EMA200 but not BULLISH                      | reduced risk / slots |
| BEARISH | close < EMA200                                      | no new entries       |
| FEAR    | VIX > vix_max or VIX up > vix_spike_pct_5d in 5 days | no new entries (overrides) |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .indicators import ema

BULLISH, CAUTION, BEARISH, FEAR = "BULLISH", "CAUTION", "BEARISH", "FEAR"
ENTRY_REGIMES = {BULLISH, CAUTION}


@dataclass
class RegimeState:
    """Regime on one day, with the numbers behind it."""

    date: date
    regime: str
    nifty_close: float
    ema_fast: float
    ema_slow: float
    vix: float | None
    vix_change_5d_pct: float | None
    allows_entries: bool
    risk_multiplier: float
    max_positions: int
    description: str


def regime_params(regime: str, cfg: Config) -> tuple[bool, float, int]:
    """(allows_entries, risk_multiplier, max_positions) for a regime."""
    if regime == BULLISH:
        return True, 1.0, cfg.account.max_open_positions
    if regime == CAUTION:
        return (True, cfg.market_filter.caution_risk_multiplier,
                min(cfg.market_filter.caution_max_positions, cfg.account.max_open_positions))
    return False, 0.0, 0


def compute_regime_series(
    index_df: pd.DataFrame, vix_df: pd.DataFrame | None, cfg: Config
) -> pd.DataFrame:
    """Vectorised regime for every Nifty bar (causal). Index: Nifty dates.

    Columns: close, ema_fast, ema_slow, vix, vix_chg5_pct, regime.
    """
    mf = cfg.market_filter
    out = pd.DataFrame(index=index_df.index)
    out["close"] = index_df["close"]
    out["ema_fast"] = ema(out["close"], mf.ema_fast)
    out["ema_slow"] = ema(out["close"], mf.ema_slow)
    if vix_df is not None and not vix_df.empty:
        vix = vix_df["close"].reindex(out.index.union(vix_df.index)).ffill().reindex(out.index)
    else:
        vix = pd.Series(np.nan, index=out.index)
    out["vix"] = vix
    out["vix_chg5_pct"] = (vix / vix.shift(5) - 1) * 100

    regime = np.where(
        (out["close"] > out["ema_fast"]) & (out["ema_fast"] > out["ema_slow"]),
        BULLISH,
        np.where(out["close"] > out["ema_slow"], CAUTION, BEARISH),
    )
    fear = (out["vix"] > mf.vix_max) | (out["vix_chg5_pct"] > mf.vix_spike_pct_5d)
    out["regime"] = np.where(fear.fillna(False), FEAR, regime)
    return out


def _describe(row: pd.Series, regime: str, cfg: Config) -> str:
    mf = cfg.market_filter
    vix_txt = f"VIX {row['vix']:.1f}" if pd.notna(row["vix"]) else "VIX unavailable"
    if pd.notna(row["vix_chg5_pct"]):
        vix_txt += f" ({row['vix_chg5_pct']:+.1f}% in 5 days)"
    trend = (
        f"Nifty {row['close']:,.0f} vs EMA{mf.ema_fast} {row['ema_fast']:,.0f} / "
        f"EMA{mf.ema_slow} {row['ema_slow']:,.0f}"
    )
    effect = {
        BULLISH: "uptrend intact; normal risk",
        CAUTION: f"trend mixed; risk ×{mf.caution_risk_multiplier}, max {mf.caution_max_positions} positions",
        BEARISH: "Nifty below its 200 EMA; no new entries",
        FEAR: f"volatility too high (VIX > {mf.vix_max:g} or up > {mf.vix_spike_pct_5d:g}% in 5 days); no new entries",
    }[regime]
    return f"{trend}; {vix_txt}. {effect}."


def regime_at(series: pd.DataFrame, when: date | pd.Timestamp, cfg: Config) -> RegimeState:
    """RegimeState for the latest Nifty bar on or before ``when``."""
    sub = series.loc[: pd.Timestamp(when)]
    if sub.empty:
        raise ValueError(f"No Nifty data on or before {when}")
    row = sub.iloc[-1]
    regime = str(row["regime"])
    allows, mult, max_pos = regime_params(regime, cfg)
    return RegimeState(
        date=sub.index[-1].date(),
        regime=regime,
        nifty_close=float(row["close"]),
        ema_fast=float(row["ema_fast"]),
        ema_slow=float(row["ema_slow"]),
        vix=None if pd.isna(row["vix"]) else float(row["vix"]),
        vix_change_5d_pct=None if pd.isna(row["vix_chg5_pct"]) else float(row["vix_chg5_pct"]),
        allows_entries=allows,
        risk_multiplier=mult,
        max_positions=max_pos,
        description=_describe(row, regime, cfg),
    )


def regime_change_message(previous: str | None, current: str) -> str | None:
    """Text for a REGIME_CHANGE alert, or None if unchanged / no previous regime."""
    if previous is None or previous == current:
        return None
    return f"Market regime changed from {previous} to {current}."
