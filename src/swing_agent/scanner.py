"""Strategy v1 "Pullback in an uptrend": entry rules, trade plans and ranking.

The rules are computed vectorised over a whole price history by ``setup_frame`` (each
value uses only bars up to that day), so the live scan and the backtester call exactly
the same functions: ``setup_frame`` → ``evaluate_row`` → ``rank_and_select``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import pandas as pd

from .config import Config
from .events import check_results
from .fmt import inr
from .market_filter import RegimeState

log = logging.getLogger(__name__)

SETUP_NAME = "PULLBACK_UPTREND"
CONDITIONS = ["universe", "uptrend", "pullback", "touch", "rsi", "trigger"]
CONDITION_LABELS = {
    "universe": "failed universe filter (price / liquidity / history)",
    "uptrend": "not in an uptrend",
    "pullback": "no 3–12% pullback",
    "touch": "didn't touch 20 EMA or closed below 50 EMA",
    "rsi": "RSI didn't reset and turn up",
    "trigger": "no bullish trigger bar",
}

# A score function maps one setup_frame row to a ranking score (pluggable for v3 ML ranker).
ScoreFn = Callable[[pd.Series], float]


def rule_score(row: pd.Series) -> float:
    """Default score: relative strength vs Nifty + 2 × bonus points."""
    rs = row.get("rs")
    return float((0.0 if pd.isna(rs) else rs) + 2 * row["bonus"])


# ---------------------------------------------------------------- vectorised rules

def setup_frame(ind: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Add entry-condition columns (c_*), ``setup`` and ``bonus`` to an indicator frame."""
    e, u, st = cfg.entry, cfg.universe, cfg.stops_targets
    out = ind.copy()
    c, h, lo, o = out["close"], out["high"], out["low"], out["open"]

    out["high_lb"] = h.rolling(e.pullback_lookback, min_periods=e.pullback_lookback).max()
    out["low_lb"] = lo.rolling(e.pullback_low_lookback, min_periods=e.pullback_low_lookback).min()
    out["pullback_pct"] = (out["high_lb"] - out["low_lb"]) / out["high_lb"] * 100
    out["touch_low"] = lo.rolling(e.touch_lookback, min_periods=e.touch_lookback).min()
    out["rsi_min"] = out["rsi"].rolling(e.rsi_dip_lookback, min_periods=e.rsi_dip_lookback).min()
    out["rsi_prev"] = out["rsi"].shift(1)
    out["prev_high"] = h.shift(1)
    out["swing_low"] = lo.rolling(st.swing_low_lookback, min_periods=st.swing_low_lookback).min()
    out["vol_dry_prev"] = out["volume"].astype(float).shift(1).rolling(e.volume_dryup_days).mean()

    out["c_universe"] = (c >= u.min_price) & (out["atv_cr"] >= u.min_avg_traded_value_cr) & (
        out["bars"] >= u.min_history_bars
    )
    out["c_uptrend"] = (c > out["ema_l"]) & (out["ema_m"] > out["ema_l"]) & (
        out["ema_l"] > out["ema_l"].shift(e.ema_long_slope_lookback)
    )
    out["c_pullback"] = (out["pullback_pct"] >= e.pullback_min_pct) & (out["pullback_pct"] <= e.pullback_max_pct)
    out["c_touch"] = (out["touch_low"] <= out["ema_s"] * (1 + e.touch_tolerance_pct / 100)) & (c > out["ema_m"])
    out["c_rsi"] = (out["rsi_min"] >= e.rsi_dip_min) & (out["rsi_min"] <= e.rsi_dip_max) & (
        out["rsi"] > out["rsi_prev"]
    )
    out["c_trigger"] = (c > out["prev_high"]) & (c > out["ema_s"]) & (c > o)
    out["setup"] = out[[f"c_{k}" for k in CONDITIONS]].all(axis=1)

    out["bonus_dryup"] = (out["vol_dry_prev"] < out["vol_avg_prev"]).astype(int)
    out["bonus_volume"] = (out["volume"] > out["vol_avg_prev"]).astype(int)
    out["bonus"] = out["bonus_dryup"] + out["bonus_volume"]
    return out


def first_failed_condition(row: pd.Series) -> str | None:
    """Name of the first entry condition that fails on this row, or None if all pass."""
    for k in CONDITIONS:
        if not bool(row[f"c_{k}"]):
            return k
    return None


# ---------------------------------------------------------------- trade plan

@dataclass
class TradePlan:
    """Entry, stop and target for one trade."""

    entry: float
    stop: float
    target: float
    r: float
    stop_pct: float
    stop_widened_to_atr: bool


def plan_trade(entry: float, swing_low: float, atr: float, cfg: Config) -> tuple[TradePlan | None, str | None]:
    """Stop below the recent swing low (at least ``min_stop_atr`` ATR away); target = entry + target_r × R.

    Returns (plan, None) or (None, rejection reason).
    """
    st = cfg.stops_targets
    stop = swing_low * (1 - st.stop_buffer_pct / 100)
    widened = False
    if pd.notna(atr) and entry - stop < st.min_stop_atr * atr:
        stop = entry - st.min_stop_atr * atr
        widened = True
    r = entry - stop
    if r <= 0:
        return None, "invalid stop (not below entry)"
    stop_pct = r / entry * 100
    if stop_pct > st.max_stop_pct:
        return None, f"stop too wide ({stop_pct:.1f}% > {st.max_stop_pct:g}%)"
    return TradePlan(entry, stop, entry + st.target_r * r, r, stop_pct, widened), None


# ---------------------------------------------------------------- signals

@dataclass
class Signal:
    """A buy setup on day T with its plan for day T+1."""

    symbol: str
    name: str
    sector: str
    date: date
    signal_close: float
    buy_zone_low: float
    buy_zone_high: float
    stop: float
    target: float
    r: float
    stop_pct: float
    rr: float
    score: float
    rs: float | None
    atr: float
    swing_low: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    setup: str = SETUP_NAME
    status: str = "WATCH"   # TAKE | WATCH
    status_note: str = ""
    # Filled by the risk manager (milestone 3)
    qty: int | None = None
    risk_amount: float | None = None
    est_costs: float | None = None
    signal_id: int | None = None   # set once stored in the signals table
    explanation: str | None = None # optional plain-English text from the local LLM (v2)

    @property
    def entry_plan(self) -> float:
        """Planning entry = top of the buy zone (conservative)."""
        return self.buy_zone_high


def make_reasons(row: pd.Series, cfg: Config) -> list[str]:
    """Human-readable reasons for a setup row (also the non-AI explanation)."""
    e = cfg.entry
    reasons = [
        f"Uptrend: price {inr(row['close'])} above {e.ema_long} EMA ({inr(row['ema_l'])}); "
        f"{e.ema_mid} EMA above {e.ema_long} EMA and the {e.ema_long} EMA is rising",
        f"Pulled back {row['pullback_pct']:.1f}% from {e.pullback_lookback}-day high ({inr(row['high_lb'])})",
        f"Touched {e.ema_short} EMA support (low {inr(row['touch_low'])} vs EMA {inr(row['ema_s'])}) "
        f"and holding above {e.ema_mid} EMA ({inr(row['ema_m'])})",
        f"RSI dipped to {row['rsi_min']:.0f} and is turning up (now {row['rsi']:.0f})",
        f"Bullish trigger: closed {inr(row['close'])} above yesterday's high {inr(row['prev_high'])}",
    ]
    if row["bonus_dryup"]:
        reasons.append("Volume dried up during the pullback")
    if row["bonus_volume"] and row["vol_avg_prev"] > 0:
        reasons.append(f"Volume {row['volume'] / row['vol_avg_prev']:.2f}× its {e.volume_avg_days}-day average today")
    if pd.notna(row.get("rs")):
        reasons.append(f"Relative strength vs Nifty ({e.rs_lookback}d): {row['rs']:+.1f} pts")
    return reasons


def evaluate_row(
    symbol: str,
    row: pd.Series,
    asof: date,
    cfg: Config,
    name: str = "",
    sector: str = "Unknown",
    results_dates: list[date] | None = None,
    check_results_dates: bool = True,
    score_fn: ScoreFn = rule_score,
) -> tuple[Signal | None, str | None]:
    """Turn one setup_frame row (day T) into a Signal, or return the rejection reason.

    Regime is not checked here (see ``scan``); results-date blocking is skipped when
    ``check_results_dates`` is False (the backtester has no historical results dates).
    """
    failed = first_failed_condition(row)
    if failed:
        return None, CONDITION_LABELS[failed]

    warnings: list[str] = []
    if check_results_dates:
        rc = check_results(results_dates, asof)
        if rc.unknown:
            warnings.append("results date unknown")
        elif rc.within(cfg.entry.avoid_results_within_days):
            return None, f"results on {rc.next_date} ({rc.trading_days_away} trading days away)"
        elif rc.within(cfg.stops_targets.time_stop_days):
            warnings.append(f"results on {rc.next_date} ({rc.trading_days_away} trading days away)")

    zone_low = float(row["close"])
    zone_high = float(row["high"]) * (1 + cfg.entry.buy_zone_top_buffer_pct / 100)
    plan, why = plan_trade(zone_high, float(row["swing_low"]), float(row["atr"]), cfg)
    if plan is None:
        return None, why
    if plan.stop_widened_to_atr:
        warnings.append(f"stop widened to {cfg.stops_targets.min_stop_atr:g}× ATR below entry")

    sig = Signal(
        symbol=symbol,
        name=name,
        sector=sector or "Unknown",
        date=asof,
        signal_close=zone_low,
        buy_zone_low=zone_low,
        buy_zone_high=zone_high,
        stop=plan.stop,
        target=plan.target,
        r=plan.r,
        stop_pct=plan.stop_pct,
        rr=(plan.target - plan.entry) / plan.r,
        score=score_fn(row),
        rs=None if pd.isna(row.get("rs")) else float(row["rs"]),
        atr=float(row["atr"]),
        swing_low=float(row["swing_low"]),
        reasons=make_reasons(row, cfg),
        warnings=warnings,
    )
    return sig, None


# ---------------------------------------------------------------- scan + ranking

@dataclass
class ScanResult:
    """Everything the live scan found on one day."""

    asof: date
    regime: RegimeState
    signals: list[Signal]
    rejected: list[tuple[str, str]]
    funnel: dict[str, int]
    stale: list[str]
    scanned: int


def scan(
    frames: dict[str, pd.DataFrame],
    asof: date,
    regime: RegimeState,
    cfg: Config,
    meta: pd.DataFrame,
    results_lookup: dict[str, list[date]] | None = None,
    check_results_dates: bool = True,
    score_fn: ScoreFn = rule_score,
) -> ScanResult:
    """Evaluate every symbol's setup_frame on ``asof``. Symbols without a bar on ``asof`` are stale."""
    meta_idx = meta.set_index("symbol") if "symbol" in meta.columns else meta
    ts = pd.Timestamp(asof)
    signals: list[Signal] = []
    rejected: list[tuple[str, str]] = []
    stale: list[str] = []
    funnel = {k: 0 for k in CONDITIONS}
    funnel["passed"] = 0

    for sym, frame in frames.items():
        if ts not in frame.index:
            stale.append(sym)
            continue
        row = frame.loc[ts]
        failed = first_failed_condition(row)
        if failed:
            funnel[failed] += 1
            continue
        funnel["passed"] += 1
        name = str(meta_idx.at[sym, "name"]) if sym in meta_idx.index else ""
        sector = str(meta_idx.at[sym, "sector"]) if sym in meta_idx.index else "Unknown"
        sig, why = evaluate_row(
            sym, row, asof, cfg, name, sector,
            results_dates=(results_lookup or {}).get(sym),
            check_results_dates=check_results_dates,
            score_fn=score_fn,
        )
        if sig is None:
            rejected.append((sym, why or "rejected"))
        elif not regime.allows_entries:
            rejected.append((sym, f"setup found, but regime is {regime.regime}: no new entries"))
        else:
            if regime.risk_multiplier < 1:
                sig.warnings.append(f"market in {regime.regime}: risk ×{regime.risk_multiplier:g}")
            signals.append(sig)

    return ScanResult(asof, regime, signals, rejected, funnel, stale, len(frames))


def rank_and_select(
    signals: list[Signal],
    max_positions: int,
    held: set[str] | None = None,
    open_sector_counts: dict[str, int] | None = None,
    max_per_sector: int = 2,
) -> list[Signal]:
    """Sort by score and mark TAKE (up to free slots, respecting sector limits) or WATCH."""
    held = held or set()
    sectors = dict(open_sector_counts or {})
    free = max(0, max_positions - len(held))
    taken = 0
    ranked = sorted(signals, key=lambda s: s.score, reverse=True)
    for s in ranked:
        if s.symbol in held:
            s.status, s.status_note = "WATCH", "already held"
        elif sectors.get(s.sector, 0) >= max_per_sector:
            s.status, s.status_note = "WATCH", f"sector limit ({max_per_sector}) reached for {s.sector}"
        elif taken >= free:
            s.status, s.status_note = "WATCH", "no free position slot"
        else:
            s.status, s.status_note = "TAKE", ""
            taken += 1
            sectors[s.sector] = sectors.get(s.sector, 0) + 1
    return ranked
