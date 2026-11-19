"""Hand-shaped price patterns for scanner and regime tests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _bars(closes: list[float], opens: list[float] | None = None, vols: list[float] | None = None,
          start: str = "2020-01-01", wick: float = 0.003) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens if opens is not None else np.r_[closes[0], closes[:-1]], dtype=float)
    vols = np.asarray(vols if vols is not None else [1_000_000] * len(closes), dtype=float)
    high = np.maximum(opens, closes) * (1 + wick)
    low = np.minimum(opens, closes) * (1 - wick)
    idx = pd.bdate_range(start=start, periods=len(closes), name="date")
    return pd.DataFrame({"open": opens, "high": high, "low": low, "close": closes,
                         "volume": vols.astype("int64")}, index=idx)


def uptrend_closes(n: int = 280, start: float = 500.0, daily: float = 0.003) -> list[float]:
    """Steady uptrend with a small alternating wiggle so RSI isn't pinned at 100."""
    out, p = [], start
    for i in range(n):
        p *= 1 + daily + (0.006 if i % 2 == 0 else -0.006)
        out.append(p)
    return out


def pullback_series(depth_steps: int = 4, step_pct: float = 1.3, trigger_pct: float = 3.0,
                    n_trend: int = 280, dry_volume: bool = True) -> pd.DataFrame:
    """Uptrend → ``depth_steps`` down days of ``step_pct`` % → one bullish trigger bar (last bar)."""
    closes = uptrend_closes(n_trend)
    vols = [1_000_000] * n_trend
    p = closes[-1]
    for _ in range(depth_steps):
        p *= 1 - step_pct / 100
        closes.append(p)
        vols.append(600_000 if dry_volume else 1_000_000)
    prev_close = closes[-1]
    prev_high = max(closes[-2], closes[-1]) * 1.003
    trig_close = max(prev_close * (1 + trigger_pct / 100), prev_high * 1.002)
    closes.append(trig_close)
    vols.append(1_500_000)
    opens = list(np.r_[closes[0], closes[:-1]])
    return _bars(closes, opens, vols)


def downtrend_series(n: int = 300) -> pd.DataFrame:
    """Mirror of the uptrend: steadily falling prices, ending with a bounce bar."""
    closes = [1000.0]
    for i in range(n - 1):
        closes.append(closes[-1] * (1 - 0.003 + (0.006 if i % 2 == 0 else -0.006)))
    closes.append(closes[-1] * 1.02)
    return _bars(closes)


def index_series(kind: str, n: int = 300) -> pd.DataFrame:
    """Synthetic Nifty: 'bull' (steady rise), 'caution' (rise then dip below EMA50, above EMA200),
    'bear' (fall)."""
    if kind == "bull":
        closes = uptrend_closes(n, 10000, 0.002)
    elif kind == "bear":
        closes = [10000 * (1 - 0.002) ** i for i in range(n)]
    elif kind == "caution":
        closes = uptrend_closes(n - 15, 10000, 0.002)
        for _ in range(15):
            closes.append(closes[-1] * 0.993)
    else:
        raise ValueError(kind)
    return _bars(closes, wick=0.002)
