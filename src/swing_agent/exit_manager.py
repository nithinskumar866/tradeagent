"""Exit rules for open positions, checked on the latest bar in priority order.

1 STOP_HIT     low ≤ stop
2 TARGET_HIT   high ≥ target
3 MOVE_STOP    close ≥ entry + breakeven_at_r × R and stop < entry
4 TREND_EXIT   close < EMA(trend_exit_ema)
5 TIME_EXIT    held ≥ time_stop_days trading days
6 RESULTS_SOON RESULTS event within results_warning_days
– HOLD

``exit_rule`` is the pure rule check shared with the backtester.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .config import Config
from .events import check_results
from .fmt import inr
from .market_filter import RegimeState
from .portfolio import Position

STOP_HIT, TARGET_HIT, MOVE_STOP = "STOP_HIT", "TARGET_HIT", "MOVE_STOP"
TREND_EXIT, TIME_EXIT, RESULTS_SOON = "TREND_EXIT", "TIME_EXIT", "RESULTS_SOON"
HOLD = "HOLD"

# Most urgent first (used to sort alerts in reports).
URGENCY = [STOP_HIT, TARGET_HIT, TREND_EXIT, TIME_EXIT, RESULTS_SOON, MOVE_STOP, "REGIME_CHANGE", "DATA"]
EXIT_NOW = {STOP_HIT, TARGET_HIT, TREND_EXIT, TIME_EXIT}


def exit_rule(
    low: float,
    high: float,
    close: float,
    ema_exit: float,
    entry: float,
    stop: float,
    target: float | None,
    r_per_share: float | None,
    bars_held: int,
    cfg: Config,
    results_soon: bool = False,
) -> str:
    """Return the highest-priority exit rule that fires on this bar, or HOLD."""
    st = cfg.stops_targets
    if low <= stop:
        return STOP_HIT
    if target is not None and high >= target:
        return TARGET_HIT
    if r_per_share and stop < entry and close >= entry + st.breakeven_at_r * r_per_share:
        return MOVE_STOP
    if pd.notna(ema_exit) and close < ema_exit:
        return TREND_EXIT
    if bars_held >= st.time_stop_days:
        return TIME_EXIT
    if results_soon:
        return RESULTS_SOON
    return HOLD


@dataclass
class Alert:
    """One alert row for the alerts table / report."""

    symbol: str
    type: str
    message: str
    suggested_action: str

    def as_dict(self) -> dict:
        """Dict for db.replace_run_outputs."""
        return {"symbol": self.symbol, "type": self.type, "message": self.message,
                "suggested_action": self.suggested_action}


@dataclass
class PositionStatus:
    """Daily check result for one open position."""

    symbol: str
    qty: int
    entry: float
    stop: float | None
    target: float | None
    last_date: date | None
    last_close: float | None
    pnl: float | None
    pnl_pct: float | None
    r_now: float | None
    days_held: int
    action: str
    alert: Alert | None
    notes: list[str] = field(default_factory=list)


def _alert(sym: str, action: str, pos: Position, bar: pd.Series, cfg: Config, results_date: date | None) -> Alert:
    st = cfg.stops_targets
    if action == STOP_HIT:
        return Alert(sym, action, f"{sym}: stop-loss {inr(pos.stop)} was hit (low {inr(bar['low'])}).",
                     "If not already out, sell at tomorrow's open.")
    if action == TARGET_HIT:
        return Alert(sym, action, f"{sym}: target {inr(pos.target)} reached (high {inr(bar['high'])}).",
                     "Book profit.")
    if action == MOVE_STOP:
        return Alert(sym, action, f"{sym}: up {st.breakeven_at_r:g}R or more (close {inr(bar['close'])}).",
                     f"Move stop-loss to breakeven {inr(pos.avg_entry)}.")
    if action == TREND_EXIT:
        return Alert(sym, action, f"{sym}: trend broken: closed {inr(bar['close'])} below "
                     f"{st.trend_exit_ema} EMA ({inr(bar['ema_exit'])}).", "Sell at tomorrow's open.")
    if action == TIME_EXIT:
        return Alert(sym, action, f"{sym}: trade hasn't worked in {st.time_stop_days} trading days.",
                     "Consider exiting.")
    return Alert(sym, RESULTS_SOON, f"{sym}: results on {results_date}.", "Consider exiting or reducing size.")


def check_position(
    pos: Position,
    frame: pd.DataFrame | None,
    asof: date,
    cfg: Config,
    results_dates: list[date] | None = None,
    regime: RegimeState | None = None,
) -> PositionStatus:
    """Evaluate exit rules for one open position on the latest bar ≤ ``asof``."""
    sym = pos.symbol
    notes: list[str] = []
    if regime is not None and not regime.allows_entries:
        notes.append("Market weak. Consider tightening stops.")
    base = dict(symbol=sym, qty=pos.qty, entry=pos.avg_entry, stop=pos.stop, target=pos.target)

    bars = frame.loc[: pd.Timestamp(asof)] if frame is not None else None
    if bars is None or bars.empty:
        a = Alert(sym, "DATA", f"{sym}: no price data available.", "Check the symbol and run update.")
        return PositionStatus(**base, last_date=None, last_close=None, pnl=None, pnl_pct=None, r_now=None,
                              days_held=0, action="DATA", alert=a, notes=notes)

    bar = bars.iloc[-1]
    close = float(bar["close"])
    days_held = int((bars.index > pd.Timestamp(pos.entry_date)).sum())
    pnl = (close - pos.avg_entry) * pos.qty
    r_ps = pos.r_per_share
    status = dict(last_date=bars.index[-1].date(), last_close=close, pnl=pnl,
                  pnl_pct=(close / pos.avg_entry - 1) * 100,
                  r_now=(close - pos.avg_entry) / r_ps if r_ps else None, days_held=days_held)

    if pos.stop is None:
        a = Alert(sym, "DATA", f"{sym}: no stop-loss recorded, so exits can't be checked.",
                  "Set one with `swing.py trade move-stop`.")
        return PositionStatus(**base, **status, action="DATA", alert=a, notes=notes)

    rc = check_results(results_dates, status["last_date"])
    action = exit_rule(
        low=float(bar["low"]), high=float(bar["high"]), close=close, ema_exit=float(bar["ema_exit"]),
        entry=pos.avg_entry, stop=pos.stop, target=pos.target, r_per_share=r_ps, bars_held=days_held,
        cfg=cfg, results_soon=rc.within(cfg.stops_targets.results_warning_days),
    )
    alert = None if action == HOLD else _alert(sym, action, pos, bar, cfg, rc.next_date)
    return PositionStatus(**base, **status, action=action, alert=alert, notes=notes)


def sort_alerts(alerts: list[Alert]) -> list[Alert]:
    """Most urgent first."""
    rank = {t: i for i, t in enumerate(URGENCY)}
    return sorted(alerts, key=lambda a: rank.get(a.type, len(URGENCY)))
