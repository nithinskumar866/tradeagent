"""Technical indicators implemented in pandas.

Every function is causal: the value at bar t uses only bars ≤ t (no look-ahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Config


def ema(close: pd.Series, n: int) -> pd.Series:
    """Exponential moving average: ``close.ewm(span=n, adjust=False).mean()``."""
    return close.ewm(span=n, adjust=False).mean()


def sma(x: pd.Series, n: int) -> pd.Series:
    """Simple moving average over ``n`` bars (NaN until ``n`` bars exist)."""
    return x.rolling(n, min_periods=n).mean()


def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI with Wilder's smoothing (``ewm(alpha=1/n, adjust=False)``). Range [0, 100]."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    # No losses at all → RSI 100; flat (no gains, no losses) → 50.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi.where(avg_gain.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    """True range; the first bar uses high − low."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average true range with Wilder's smoothing."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def avg_traded_value_cr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """``n``-day mean of close × volume, in crore INR (1 crore = 1e7)."""
    return sma(df["close"] * df["volume"] / 1e7, n)


def relative_strength(stock_close: pd.Series, index_close: pd.Series, n: int = 63) -> pd.Series:
    """``n``-bar stock return minus ``n``-bar index return, in percentage points.

    The index is aligned to the stock's dates (forward-filled), so only past index
    values are used.
    """
    idx = index_close.reindex(stock_close.index.union(index_close.index)).ffill().reindex(stock_close.index)
    return (stock_close.pct_change(n, fill_method=None) - idx.pct_change(n, fill_method=None)) * 100


def add_all_indicators(
    df: pd.DataFrame, cfg: Config, index_close: pd.Series | None = None
) -> pd.DataFrame:
    """Return a copy of an OHLCV frame (DatetimeIndex) with all indicator columns added.

    Columns: ema_s, ema_m, ema_l (entry EMAs), ema_exit (trend-exit EMA), rsi, atr,
    vol_avg_prev (avg volume of the ``volume_avg_days`` bars before today), atv_cr, rs, bars.
    """
    e, st = cfg.entry, cfg.stops_targets
    out = df.copy()
    c = out["close"]
    out["ema_s"] = ema(c, e.ema_short)
    out["ema_m"] = ema(c, e.ema_mid)
    out["ema_l"] = ema(c, e.ema_long)
    out["ema_exit"] = ema(c, st.trend_exit_ema)
    out["rsi"] = rsi_wilder(c, e.rsi_period)
    out["atr"] = atr_wilder(out, st.atr_period)
    out["vol_avg_prev"] = sma(out["volume"].astype(float), e.volume_avg_days).shift(1)
    out["atv_cr"] = avg_traded_value_cr(out, 20)
    out["rs"] = relative_strength(c, index_close, e.rs_lookback) if index_close is not None else np.nan
    out["bars"] = np.arange(1, len(out) + 1)
    return out
