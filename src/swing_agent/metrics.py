"""Performance statistics for backtests (and the user's real journal)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd


def cagr(first: float, last: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Compound annual growth rate in % between two equity values."""
    years = (end - start).days / 365.25
    if years <= 0 or first <= 0:
        return float("nan")
    return ((last / first) ** (1 / years) - 1) * 100


def drawdown(equity: pd.Series) -> pd.Series:
    """Drawdown from the running peak, in % (≤ 0)."""
    return (equity / equity.cummax() - 1) * 100


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """(max drawdown %, longest time under water in calendar days)."""
    if equity.empty:
        return 0.0, 0
    dd = drawdown(equity)
    longest, start = 0, None
    for ts, v in dd.items():
        if v < 0 and start is None:
            start = ts
        elif v >= 0 and start is not None:
            longest = max(longest, (ts - start).days)
            start = None
    if start is not None:
        longest = max(longest, (dd.index[-1] - start).days)
    return float(dd.min()), int(longest)


def longest_losing_streak(pnls: pd.Series) -> int:
    """Most consecutive losing trades (in exit order)."""
    best = cur = 0
    for p in pnls:
        cur = cur + 1 if p <= 0 else 0
        best = max(best, cur)
    return best


def trade_stats(trades: pd.DataFrame) -> dict:
    """Win rate, average win/loss (₹ and R), profit factor, expectancy, streaks, holding."""
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate_pct": float("nan"), "avg_win": float("nan"), "avg_loss": float("nan"),
                "avg_win_r": float("nan"), "avg_loss_r": float("nan"), "profit_factor": float("nan"),
                "expectancy_r": float("nan"), "longest_losing_streak": 0, "avg_holding_bars": float("nan")}
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] <= 0]
    gross_loss = -losses["pnl"].sum()
    ordered = trades.sort_values("exit_date") if "exit_date" in trades else trades
    return {
        "trades": n,
        "win_rate_pct": len(wins) / n * 100,
        "avg_win": wins["pnl"].mean() if len(wins) else float("nan"),
        "avg_loss": losses["pnl"].mean() if len(losses) else float("nan"),
        "avg_win_r": wins["r_multiple"].mean() if len(wins) else float("nan"),
        "avg_loss_r": losses["r_multiple"].mean() if len(losses) else float("nan"),
        "profit_factor": wins["pnl"].sum() / gross_loss if gross_loss > 0 else float("inf"),
        "expectancy_r": trades["r_multiple"].mean(),
        "longest_losing_streak": longest_losing_streak(ordered["pnl"]),
        "avg_holding_bars": trades["bars_held"].mean() if "bars_held" in trades else float("nan"),
    }


def period_metrics(equity: pd.DataFrame, trades: pd.DataFrame) -> dict:
    """All metrics for one period. ``equity`` should start at the period's base value."""
    eq, bench = equity["equity"], equity["benchmark"]
    s, e = eq.index[0], eq.index[-1]
    dd, dd_days = max_drawdown(eq)
    bdd, _ = max_drawdown(bench)
    out = {
        "start": s.date(), "end": e.date(),
        "total_return_pct": (eq.iloc[-1] / eq.iloc[0] - 1) * 100,
        "cagr_pct": cagr(eq.iloc[0], eq.iloc[-1], s, e),
        "max_drawdown_pct": dd, "max_drawdown_days": dd_days,
        "time_invested_pct": float((equity["positions"].iloc[1:] > 0).mean() * 100) if len(equity) > 1 else 0.0,
        "nifty_total_return_pct": (bench.iloc[-1] / bench.iloc[0] - 1) * 100,
        "nifty_cagr_pct": cagr(bench.iloc[0], bench.iloc[-1], s, e),
        "nifty_max_drawdown_pct": bdd,
    }
    out.update(trade_stats(trades))
    return out


def split_periods(equity: pd.DataFrame, trades: pd.DataFrame, in_sample_end: date) -> dict[str, tuple]:
    """{'In-sample', 'Out-of-sample', 'Full period'} → (equity slice, trades by entry date).

    The out-of-sample equity starts from the last in-sample day so its first day's change counts.
    """
    cut = pd.Timestamp(in_sample_end)
    entry = pd.to_datetime(trades["entry_date"]) if len(trades) else pd.Series(dtype="datetime64[ns]")
    out: dict[str, tuple] = {}
    ins = equity[equity.index <= cut]
    if len(ins) >= 2:
        out["In-sample"] = (ins, trades[entry <= cut])
    oos_start = ins.index[-1] if len(ins) else equity.index[0]
    oos = equity[equity.index >= oos_start]
    if len(oos) >= 2 and equity.index[-1] > cut:
        out["Out-of-sample"] = (oos, trades[entry > cut])
    out["Full period"] = (equity, trades)
    return out


def yearly_returns(equity: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year % return of the strategy and of Nifty buy-and-hold."""
    rows = []
    prev_eq = prev_b = None
    for year, g in equity.groupby(equity.index.year):
        base_eq = prev_eq if prev_eq is not None else g["equity"].iloc[0]
        base_b = prev_b if prev_b is not None else g["benchmark"].iloc[0]
        rows.append({"year": int(year),
                     "strategy_pct": (g["equity"].iloc[-1] / base_eq - 1) * 100,
                     "nifty_pct": (g["benchmark"].iloc[-1] / base_b - 1) * 100,
                     "trades_open_days_pct": float((g["positions"] > 0).mean() * 100)})
        prev_eq, prev_b = g["equity"].iloc[-1], g["benchmark"].iloc[-1]
    return pd.DataFrame(rows)


def fmt_metric(key: str, v) -> str:
    """Pretty print one metric value."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "∞" if isinstance(v, float) and math.isinf(v) else "–"
    if key.endswith("_pct"):
        return f"{v:.1f}%"
    if key.endswith("_r") or key == "profit_factor":
        return f"{v:.2f}"
    if key in ("avg_win", "avg_loss"):
        from .fmt import inr

        return inr(float(v), 0)
    if isinstance(v, (float, np.floating)):
        return f"{v:.1f}"
    return str(v)
