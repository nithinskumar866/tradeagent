"""Orchestrates the daily routine.

``scan``: load prices → indicators → regime → signals → sizing → exit checks (no writes).
``run``:  update data first, then the same, then save signals/alerts/report (idempotent per day).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import db
from .config import PROJECT_ROOT, Config
from .data_fetcher import DataFetcher, UpdateReport
from .data_sources import DataSource, get_source
from .events import get_events, refresh_earnings, results_dates_by_symbol, sync_events_csv
from .exit_manager import Alert, PositionStatus, check_position, sort_alerts
from .indicators import add_all_indicators
from .market_filter import compute_regime_series, regime_at, regime_change_message
from .portfolio import Portfolio, build_portfolio
from .risk import size_signals
from .scanner import ScanResult, Signal, rank_and_select, scan, setup_frame
from .watchlist import load_watchlist

log = logging.getLogger(__name__)

REPORTS_DIR = PROJECT_ROOT / "reports"


class NoDataError(Exception):
    """Raised when the DB has no Nifty data yet (run `swing.py init`)."""


@dataclass
class MarketData:
    """Everything the strategy needs, computed from the DB."""

    frames: dict[str, pd.DataFrame]   # symbol → setup_frame (indicators + conditions)
    regime_series: pd.DataFrame
    meta: pd.DataFrame                # symbol, name, sector
    latest_trading_day: date

    @property
    def sectors(self) -> dict[str, str]:
        """symbol → sector."""
        return dict(zip(self.meta["symbol"], self.meta["sector"]))


def load_market_data(
    conn: sqlite3.Connection, cfg: Config, symbols: list[str] | None = None,
    extra_symbols: list[str] | None = None,
) -> MarketData:
    """Read prices from the DB and compute indicators, setup conditions and regime.

    ``extra_symbols`` (e.g. held positions no longer on the watchlist) are loaded too.
    """
    index_df = db.get_prices(conn, cfg.data.index_symbol)
    if index_df.empty:
        raise NoDataError("No Nifty data in the database. Run `python swing.py init` first.")
    vix_df = db.get_prices(conn, cfg.data.vix_symbol)
    meta = db.get_symbols(conn, active_only=False)
    active = set(db.get_symbols(conn)["symbol"])
    wanted = set(symbols) if symbols is not None else active
    wanted |= set(extra_symbols or [])
    meta = meta[meta["symbol"].isin(wanted)]

    frames: dict[str, pd.DataFrame] = {}
    for sym in sorted(wanted):
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
    scan_symbols: set[str] | None = None,
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

    frames = market.frames if scan_symbols is None else {
        s: f for s, f in market.frames.items() if s in scan_symbols}
    result = scan(frames, regime.date, regime, cfg, market.meta,
                  results_lookup=results_dates_by_symbol(conn))
    result.signals = rank_and_select(
        result.signals,
        max_positions=regime.max_positions,
        held=held,
        open_sector_counts=open_sector_counts,
        max_per_sector=cfg.account.max_per_sector,
    )
    return result, prev_regime


# ---------------------------------------------------------------- daily routine

@dataclass
class DailyResult:
    """Everything produced by one daily scan/run."""

    run_date: date
    scan: ScanResult
    prev_regime: str | None
    portfolio: Portfolio
    positions: list[PositionStatus]
    alerts: list[Alert]
    events: pd.DataFrame
    data_warnings: list[str] = field(default_factory=list)
    update: UpdateReport | None = None
    report_path: Path | None = None


def _signal_row(s: Signal) -> dict:
    return {
        "symbol": s.symbol, "setup": s.setup, "signal_close": s.signal_close,
        "buy_zone_low": s.buy_zone_low, "buy_zone_high": s.buy_zone_high,
        "stop": s.stop, "target": s.target, "qty": s.qty, "risk_amount": s.risk_amount,
        "rr": s.rr, "score": s.score, "reasons_json": json.dumps(s.reasons, ensure_ascii=False),
        "warnings_json": json.dumps(s.warnings, ensure_ascii=False), "regime": None,
        "status": s.status, "status_note": s.status_note, "explanation": s.explanation,
    }


def _data_warnings(update: UpdateReport | None, result: ScanResult, today: date) -> list[str]:
    out: list[str] = []
    if update is not None:
        if update.offline:
            out.append("Every download failed (no internet?). Using cached data, which may be STALE.")
        out += [i.message for i in update.issues]
    if result.stale:
        out.append(f"No bar on {result.asof} for: {', '.join(sorted(result.stale))} (skipped).")
    age = (today - result.asof).days
    if age > 4:
        out.append(f"Latest data is from {result.asof} ({age} days ago).")
    return out


def daily(
    conn: sqlite3.Connection,
    cfg: Config,
    do_update: bool = False,
    persist: bool = False,
    today: date | None = None,
    source: DataSource | None = None,
    reports_dir: Path | None = None,
    asof: date | None = None,
) -> DailyResult:
    """The daily routine. ``scan`` = (False, False); ``run`` = (True, True).

    ``asof`` evaluates a past day (read-only use; ignored when persisting).
    """
    today = today or date.today()
    update: UpdateReport | None = None
    event_warnings: list[str] = []
    if do_update:
        db.upsert_symbols(conn, load_watchlist(cfg.watchlist_path))
        update = DataFetcher(conn, source or get_source(cfg.data.source), cfg).update()
        if cfg.data.fetch_results_dates and not update.offline:
            note = refresh_earnings(conn, db.get_symbols(conn)["symbol"].tolist(), today)
            log.info(note)
            if "unavailable" in note:
                event_warnings.append(note)
    if persist:
        _, problems = sync_events_csv(conn, cfg.resolve(cfg.data.events_file))
        event_warnings += [f"{cfg.data.events_file} {p}" for p in problems]

    portfolio = build_portfolio(db.get_trades(conn), cfg.account.capital)
    market = load_market_data(conn, cfg, extra_symbols=list(portfolio.positions))
    if persist or asof is None:
        asof = market.latest_trading_day
    first_day = market.regime_series.index[0].date()
    if asof < first_day:
        raise NoDataError(f"No data on or before {asof}; stored history starts on {first_day}.")
    run_date = regime_at(market.regime_series, asof, cfg).date
    rd = run_date.isoformat()
    if persist:
        db.start_run(conn, rd, datetime.now().isoformat(timespec="seconds"))

    try:
        active = set(db.get_symbols(conn)["symbol"])
        result, prev_day = run_scan(
            conn, cfg, asof=run_date, market=market, held=set(portfolio.positions),
            open_sector_counts=portfolio.sector_counts(market.sectors), scan_symbols=active,
        )
        regime = result.regime
        prev_regime = db.previous_run_regime(conn, rd) or prev_day
        size_signals(result.signals, portfolio.capital, portfolio.available_cash, cfg, regime.risk_multiplier)
        if cfg.explainer.enabled and result.signals:
            from .explainer import explain_signals

            texts, status = explain_signals(result.signals, regime, cfg)
            log.info("Explainer: %s", status)
            for s in result.signals:
                s.explanation = texts.get(s.symbol)

        results_lookup = results_dates_by_symbol(conn)
        statuses = [
            check_position(pos, market.frames.get(sym), run_date, cfg, results_lookup.get(sym), regime)
            for sym, pos in sorted(portfolio.positions.items())
        ]
        alerts = [s.alert for s in statuses if s.alert]
        msg = regime_change_message(prev_regime, regime.regime)
        if msg:
            alerts.append(Alert("ALL", "REGIME_CHANGE", msg, regime.description))
        warnings = _data_warnings(update, result, today) + event_warnings
        if update is not None:
            alerts += [Alert(sym, "DATA", f"{sym}: download failed ({why}).", "Skipped today.")
                       for sym, why in update.failed.items()]
        alerts = sort_alerts(alerts)
        events = get_events(conn, start=run_date, end=run_date + timedelta(days=7))

        out = DailyResult(run_date, result, prev_regime, portfolio, statuses, alerts, events,
                          warnings, update)
        if persist:
            rows = [_signal_row(s) | {"regime": regime.regime} for s in result.signals]
            ids = db.replace_run_outputs(conn, rd, rows, [a.as_dict() for a in alerts])
            for s, i in zip(result.signals, ids):
                s.signal_id = i
            from .report import format_daily  # local import: report depends on this module's types

            folder = reports_dir or REPORTS_DIR
            folder.mkdir(parents=True, exist_ok=True)
            out.report_path = folder / f"{rd}.md"
            out.report_path.write_text(format_daily(out, cfg), encoding="utf-8")
            db.finish_run(conn, rd, datetime.now().isoformat(timespec="seconds"), "OK",
                          rd, regime.regime,
                          "offline: cached data" if update and update.offline else None)
        return out
    except Exception as err:
        if persist:
            db.finish_run(conn, rd, datetime.now().isoformat(timespec="seconds"), "FAILED",
                          rd, None, str(err)[:500])
        raise
