"""Portfolio-level, day-by-day backtester.

It reuses the live strategy functions (no duplicated logic):
``setup_frame`` / ``evaluate_row`` / ``rank_and_select`` (scanner), ``plan_trade`` (stops and
targets), ``position_size`` (risk), ``exit_rule`` (exit manager) and the cost model.

Timing (signals on the close of day T, entries on T+1):
* T+1 entry: skip if open > buy-zone top or open ≤ stop; else fill at open × (1 + slippage),
  then recompute stop distance, R and target from the actual fill.
* Stops: exit at min(open, stop); targets: exit at max(open, target); both on one bar → stop.
* Trend and time exits fill at the next day's open. Breakeven moves apply from the next bar.
* Slippage on every fill; charges on every order.
* Results-date filters are ignored (no reliable free history of NSE results dates).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .costs import buy_charges, sell_charges
from .exit_manager import MOVE_STOP, STOP_HIT, TARGET_HIT, TIME_EXIT, TREND_EXIT, exit_rule
from .market_filter import regime_params
from .risk import position_size
from .scanner import evaluate_row, plan_trade, rank_and_select

log = logging.getLogger(__name__)

END_OF_TEST = "END_OF_TEST"


@dataclass
class Order:
    """A TAKE signal from day T waiting for T+1's open."""

    symbol: str
    sector: str
    signal_date: pd.Timestamp
    zone_high: float
    stop: float
    swing_low: float
    atr: float
    risk_multiplier: float
    max_positions: int


@dataclass
class OpenPos:
    """A simulated open position."""

    symbol: str
    sector: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry: float
    qty: int
    stop: float
    stop_initial: float
    target: float
    r: float
    buy_fees: float
    bars_held: int = 0
    pending_exit: str | None = None
    pending_stop: float | None = None
    last_close: float = 0.0


@dataclass
class BacktestResult:
    """Simulation output."""

    trades: pd.DataFrame
    equity: pd.DataFrame          # date-indexed: equity, cash, invested, positions, benchmark
    start: date
    end: date
    initial_capital: float
    notes: list[str] = field(default_factory=list)


def entry_fill(open_: float, zone_high: float, stop: float, slippage_pct: float) -> float | None:
    """Fill price for a T+1 entry, or None if skipped (gap above the zone, or open at/below stop)."""
    if open_ > zone_high or open_ <= stop:
        return None
    return open_ * (1 + slippage_pct / 100)


def exit_fill(rule: str, open_: float, stop: float, target: float, slippage_pct: float) -> float:
    """Fill price for an intrabar exit (stop or target), after slippage."""
    if rule == STOP_HIT:
        px = min(open_, stop)
    elif rule == TARGET_HIT:
        px = max(open_, target)
    else:
        px = open_
    return px * (1 - slippage_pct / 100)


def _setup_days(frames: dict[str, pd.DataFrame]) -> dict[pd.Timestamp, list[str]]:
    out: dict[pd.Timestamp, list[str]] = {}
    for sym, f in frames.items():
        for ts in f.index[f["setup"].to_numpy(dtype=bool)]:
            out.setdefault(ts, []).append(sym)
    return out


def run_backtest(
    frames: dict[str, pd.DataFrame],
    regime_series: pd.DataFrame,
    meta: pd.DataFrame,
    cfg: Config,
    start: date | None = None,
    end: date | None = None,
) -> BacktestResult:
    """Simulate the strategy over ``[start, end]`` on precomputed setup frames."""
    sectors = dict(zip(meta["symbol"], meta["sector"]))
    names = dict(zip(meta["symbol"], meta["name"]))
    slip = cfg.costs.slippage_pct
    initial = cfg.backtest.initial_capital

    days = regime_series.index
    warm = min(cfg.universe.min_history_bars, len(days) - 1)
    first_ok = days[warm]
    req_start = pd.Timestamp(start or cfg.backtest.start_date)
    eff_start = max(req_start, first_ok)
    eff_end = pd.Timestamp(end) if end else days[-1]
    days = days[(days >= eff_start) & (days <= eff_end)]
    if len(days) < 2:
        raise ValueError("Backtest period has fewer than 2 trading days of usable data.")
    notes = []
    if eff_start > req_start:
        notes.append(f"Requested start {req_start.date()} moved to {eff_start.date()} "
                     f"(first day with {cfg.universe.min_history_bars} bars of history).")

    setup_days = _setup_days(frames)
    cash = initial
    realised = 0.0
    positions: dict[str, OpenPos] = {}
    orders: list[Order] = []
    trades: list[dict] = []
    eq_rows: list[dict] = []
    regimes = regime_series["regime"]

    def close_pos(p: OpenPos, when: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash, realised
        fees = sell_charges(price, p.qty, cfg.costs).total
        cash += price * p.qty - fees
        pnl = (price - p.entry) * p.qty - p.buy_fees - fees
        realised += pnl
        trades.append({
            "symbol": p.symbol, "name": names.get(p.symbol, ""), "sector": p.sector,
            "signal_date": p.signal_date.date(), "entry_date": p.entry_date.date(),
            "entry_price": p.entry, "qty": p.qty, "stop_initial": p.stop_initial, "target": p.target,
            "exit_date": when.date(), "exit_price": price, "exit_reason": reason,
            "fees": p.buy_fees + fees, "pnl": pnl, "r_multiple": pnl / (p.qty * p.r),
            "bars_held": p.bars_held,
        })
        del positions[p.symbol]

    for d in days:
        # 1) Exits decided at yesterday's close (trend/time) fill at today's open.
        for p in list(positions.values()):
            f = frames[p.symbol]
            if p.pending_exit and d in f.index:
                close_pos(p, d, f.at[d, "open"] * (1 - slip / 100), p.pending_exit)

        # 2) Entries from yesterday's TAKE signals.
        for o in orders:
            f = frames[o.symbol]
            if o.symbol in positions or d not in f.index:
                continue
            if len(positions) >= o.max_positions:
                continue
            if sum(1 for p in positions.values() if p.sector == o.sector) >= cfg.account.max_per_sector:
                continue
            fill = entry_fill(float(f.at[d, "open"]), o.zone_high, o.stop, slip)
            if fill is None:
                continue
            plan, _ = plan_trade(fill, o.swing_low, o.atr, cfg)
            if plan is None:
                continue
            size = position_size(fill, plan.stop, initial + realised, cash, cfg, o.risk_multiplier)
            if not size.ok:
                continue
            fees = buy_charges(fill, size.qty, cfg.costs).total
            if fill * size.qty + fees > cash:
                continue
            cash -= fill * size.qty + fees
            positions[o.symbol] = OpenPos(o.symbol, o.sector, o.signal_date, d, fill, size.qty,
                                          plan.stop, plan.stop, plan.target, plan.r, fees)
        orders = []

        # 3) Intraday stop/target and end-of-day rules for every open position.
        for p in list(positions.values()):
            f = frames[p.symbol]
            if d not in f.index:
                continue
            if p.pending_stop is not None:          # breakeven move applies from this bar
                p.stop, p.pending_stop = p.pending_stop, None
            if d > p.entry_date:
                p.bars_held += 1
            row = f.loc[d]
            p.last_close = float(row["close"])
            rule = exit_rule(float(row["low"]), float(row["high"]), p.last_close, float(row["ema_exit"]),
                             p.entry, p.stop, p.target, p.r, p.bars_held, cfg)
            if rule in (STOP_HIT, TARGET_HIT):
                close_pos(p, d, exit_fill(rule, float(row["open"]), p.stop, p.target, slip), rule)
            elif rule == MOVE_STOP:
                p.pending_stop = p.entry
            elif rule in (TREND_EXIT, TIME_EXIT):
                p.pending_exit = rule

        # 4) Signals on today's close → orders for tomorrow.
        regime = str(regimes.at[d])
        allows, mult, max_pos = regime_params(regime, cfg)
        if allows and d in setup_days:
            sigs = []
            for sym in setup_days[d]:
                sig, _ = evaluate_row(sym, frames[sym].loc[d], d.date(), cfg, names.get(sym, ""),
                                      sectors.get(sym, "Unknown"), check_results_dates=False)
                if sig is not None:
                    sigs.append(sig)
            open_sectors: dict[str, int] = {}
            for p in positions.values():
                open_sectors[p.sector] = open_sectors.get(p.sector, 0) + 1
            for s in rank_and_select(sigs, max_pos, set(positions), open_sectors, cfg.account.max_per_sector):
                if s.status == "TAKE":
                    orders.append(Order(s.symbol, s.sector, d, s.buy_zone_high, s.stop, s.swing_low, s.atr,
                                        mult, max_pos))

        invested = sum(p.qty * p.last_close for p in positions.values())
        eq_rows.append({"date": d, "equity": cash + invested, "cash": cash, "invested": invested,
                        "positions": len(positions), "regime": regime,
                        "held": ",".join(sorted(positions))})

    last = days[-1]
    for p in list(positions.values()):
        close_pos(p, last, p.last_close * (1 - slip / 100), END_OF_TEST)
    if eq_rows:
        eq_rows[-1].update(equity=cash, cash=cash, invested=0.0, positions=0, held="")

    equity = pd.DataFrame(eq_rows).set_index("date")
    bench = regime_series["close"].reindex(equity.index)
    equity["benchmark"] = initial * bench / bench.iloc[0]
    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        trades_df = pd.DataFrame(columns=["symbol", "name", "sector", "signal_date", "entry_date", "entry_price",
                                          "qty", "stop_initial", "target", "exit_date", "exit_price",
                                          "exit_reason", "fees", "pnl", "r_multiple", "bars_held"])
    notes.append("Results-date filters are ignored: historical NSE results dates aren't reliably available for free.")
    if (trades_df["exit_reason"] == END_OF_TEST).any():
        notes.append("Positions still open on the last day were closed at that day's close (END_OF_TEST).")
    return BacktestResult(trades_df, equity, days[0].date(), days[-1].date(), initial, notes)


# ---------------------------------------------------------------- inputs, outputs, sweep

HONESTY_NOTES = [
    "**Survivorship bias:** today's Nifty 100 list is applied to past years. Stocks that fell out of the "
    "index (often after doing badly) are missing, which overstates results.",
    "Fills are modelled on daily bars. Real fills, slippage and gaps differ.",
    "Past performance doesn't guarantee future results.",
    "Don't keep tuning parameters until the backtest looks good (overfitting). Judge the strategy on its "
    "**out-of-sample** results.",
]


@dataclass
class PriceCache:
    """Raw prices loaded once from the DB, so sweeps can rebuild frames cheaply."""

    prices: dict[str, pd.DataFrame]
    index: pd.DataFrame
    vix: pd.DataFrame
    meta: pd.DataFrame


def load_prices(conn, cfg: Config) -> PriceCache:
    """Load watchlist, Nifty and VIX prices from the DB."""
    from . import db

    index = db.get_prices(conn, cfg.data.index_symbol)
    if index.empty:
        raise ValueError("No Nifty data in the database. Run `python swing.py init` first.")
    meta = db.get_symbols(conn)
    prices = {s: db.get_prices(conn, s) for s in meta["symbol"]}
    prices = {s: p for s, p in prices.items() if not p.empty}
    return PriceCache(prices, index, db.get_prices(conn, cfg.data.vix_symbol), meta)


def build_frames(cache: PriceCache, cfg: Config) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Indicators + setup conditions for every symbol, and the regime series."""
    from .indicators import add_all_indicators
    from .market_filter import compute_regime_series
    from .scanner import setup_frame

    frames = {s: setup_frame(add_all_indicators(p, cfg, cache.index["close"]), cfg)
              for s, p in cache.prices.items()}
    return frames, compute_regime_series(cache.index, cache.vix, cfg)


def backtest_from_cache(cache: PriceCache, cfg: Config, start: date | None = None,
                        end: date | None = None) -> BacktestResult:
    """Build frames for ``cfg`` and run the simulation."""
    frames, regime = build_frames(cache, cfg)
    return run_backtest(frames, regime, cache.meta, cfg, start, end)


def metrics_by_period(res: BacktestResult, cfg: Config) -> dict[str, dict]:
    """Metrics for in-sample, out-of-sample and full period."""
    from .metrics import period_metrics, split_periods

    return {name: period_metrics(eq, tr)
            for name, (eq, tr) in split_periods(res.equity, res.trades, cfg.backtest.in_sample_end).items()}


METRIC_LABELS = [
    ("start", "Start"), ("end", "End"),
    ("total_return_pct", "Total return"), ("cagr_pct", "CAGR"),
    ("max_drawdown_pct", "Max drawdown"), ("max_drawdown_days", "Longest drawdown (days)"),
    ("trades", "Trades"), ("win_rate_pct", "Win rate"),
    ("avg_win", "Average win"), ("avg_loss", "Average loss"),
    ("avg_win_r", "Average win (R)"), ("avg_loss_r", "Average loss (R)"),
    ("profit_factor", "Profit factor"), ("expectancy_r", "Expectancy (R per trade)"),
    ("longest_losing_streak", "Longest losing streak"), ("avg_holding_bars", "Average holding (trading days)"),
    ("time_invested_pct", "Time invested"),
    ("nifty_total_return_pct", "Nifty buy & hold: total return"), ("nifty_cagr_pct", "Nifty buy & hold: CAGR"),
    ("nifty_max_drawdown_pct", "Nifty buy & hold: max drawdown"),
]


def summary_markdown(res: BacktestResult, cfg: Config, periods: dict[str, dict]) -> str:
    """The backtest summary.md text."""
    from . import DISCLAIMER
    from .fmt import inr
    from .metrics import fmt_metric, yearly_returns

    e, st, a = cfg.entry, cfg.stops_targets, cfg.account
    names = list(periods)
    lines = [
        "# Backtest summary", "",
        f"Period {res.start} → {res.end} · initial capital {inr(res.initial_capital, 0)} · "
        f"in-sample ends {cfg.backtest.in_sample_end}", "",
        f"Strategy: pullback in an uptrend · risk {a.risk_per_trade_pct:g}%/trade · max {a.max_open_positions} "
        f"positions, {a.max_per_sector}/sector · pullback {e.pullback_min_pct:g}–{e.pullback_max_pct:g}% · "
        f"RSI dip {e.rsi_dip_min:g}–{e.rsi_dip_max:g} · target {st.target_r:g}R · max stop {st.max_stop_pct:g}% · "
        f"time stop {st.time_stop_days} days · slippage {cfg.costs.slippage_pct:g}%/side", "",
        "## Results", "",
        "| Metric | " + " | ".join(names) + " |",
        "|---|" + "---:|" * len(names),
    ]
    for key, label in METRIC_LABELS:
        lines.append(f"| {label} | " + " | ".join(fmt_metric(key, periods[n].get(key)) for n in names) + " |")

    yr = yearly_returns(res.equity)
    lines += ["", "## Yearly returns", "", "| Year | Strategy | Nifty | Days with positions |", "|---|---:|---:|---:|"]
    lines += [f"| {r.year} | {r.strategy_pct:+.1f}% | {r.nifty_pct:+.1f}% | {r.trades_open_days_pct:.0f}% |"
              for r in yr.itertuples()]
    if len(res.trades):
        by = res.trades.groupby("exit_reason").agg(trades=("pnl", "size"), pnl=("pnl", "sum"),
                                                    avg_r=("r_multiple", "mean"))
        lines += ["", "## Exits by reason", "", "| Reason | Trades | P&L | Avg R |", "|---|---:|---:|---:|"]
        lines += [f"| {k} | {int(r.trades)} | {inr(r.pnl, 0)} | {r.avg_r:+.2f} |" for k, r in by.iterrows()]
    lines += ["", "## Notes"] + [f"- {n}" for n in res.notes]
    lines += ["", "## Read this before trusting the numbers"] + [f"- {n}" for n in HONESTY_NOTES]
    lines += ["", "---", f"_{DISCLAIMER}_"]
    return "\n".join(lines)


def write_outputs(res: BacktestResult, cfg: Config, out_dir) -> dict[str, dict]:
    """Write trades.csv, equity.csv and summary.md into ``out_dir``. Returns period metrics."""
    from pathlib import Path

    from .metrics import drawdown

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    periods = metrics_by_period(res, cfg)
    res.trades.to_csv(out / "trades.csv", index=False)
    eq = res.equity.copy()
    eq["drawdown_pct"] = drawdown(eq["equity"])
    eq.to_csv(out / "equity.csv")
    (out / "summary.md").write_text(summary_markdown(res, cfg, periods), encoding="utf-8")
    return periods


def parse_sweep(specs: list[str], cfg: Config) -> list[tuple[str, str, list]]:
    """Parse ['target_r=1.5,2,2.5', ...] into (section, key, values). Keys may be 'section.key'."""
    sections = {name: getattr(cfg, name) for name in type(cfg).model_fields}
    out = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError(f"Bad sweep spec {spec!r}; use key=v1,v2,...")
        key, vals = spec.split("=", 1)
        if "." in key:
            sec, key = key.split(".", 1)
            matches = [sec] if sec in sections and key in type(sections[sec]).model_fields else []
        else:
            matches = [n for n, s in sections.items() if key in type(s).model_fields]
        if len(matches) != 1:
            raise ValueError(f"Unknown sweep key {key!r}." if not matches
                             else f"Sweep key {key!r} exists in {matches}; use section.key.")
        current = getattr(sections[matches[0]], key)
        caster = int if isinstance(current, int) and not isinstance(current, bool) else float
        out.append((matches[0], key, [caster(v) for v in vals.split(",") if v.strip()]))
    return out


def run_sweep(cache: PriceCache, cfg: Config, grid: list[tuple[str, str, list]],
              start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Run a small parameter grid; return one row of key metrics per combination."""
    import itertools

    from .config import parse_config

    rows = []
    for combo in itertools.product(*[vals for _, _, vals in grid]):
        raw = cfg.model_dump(mode="json")
        label = {}
        for (sec, key, _), v in zip(grid, combo):
            raw[sec][key] = v
            label[key] = v
        c = parse_config(raw)
        res = backtest_from_cache(cache, c, start, end)
        p = metrics_by_period(res, c)
        row = dict(label)
        for name, short in (("Full period", "full"), ("Out-of-sample", "oos")):
            m = p.get(name, {})
            row |= {f"{short}_cagr_pct": m.get("cagr_pct"), f"{short}_max_dd_pct": m.get("max_drawdown_pct"),
                    f"{short}_trades": m.get("trades"), f"{short}_win_pct": m.get("win_rate_pct"),
                    f"{short}_pf": m.get("profit_factor"), f"{short}_exp_r": m.get("expectancy_r")}
        rows.append(row)
        log.info("Sweep %s done", label)
    return pd.DataFrame(rows)
