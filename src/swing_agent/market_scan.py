"""Scan every stock listed on NSE (not just the watchlist) with the same rules.

Prices for the ~2,400 NSE equities live in their own database (``data/nse_all.db``,
about 2 years per stock) so the daily run and its watchlist are untouched. The first
run downloads everything (roughly 10–20 minutes); later runs only fetch new bars.
"""

from __future__ import annotations

import io
import logging
import math
import os
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from . import db
from .config import PROJECT_ROOT, Config
from .data_fetcher import DataFetcher
from .events import results_dates_by_symbol
from .indicators import add_all_indicators
from .market_filter import RegimeState, compute_regime_series, regime_at
from .portfolio import build_portfolio
from .risk import size_signals
from .scanner import CONDITION_LABELS, CONDITIONS, ScanResult, rank_and_select, scan, setup_frame
from .watchlist import load_watchlist

log = logging.getLogger(__name__)

NSE_EQUITY_URLS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
]
ALL_DB_PATH = Path(os.environ.get("SWING_AGENT_ALL_DB") or PROJECT_ROOT / "data" / "nse_all.db")
LIST_PATH = PROJECT_ROOT / "data" / "watchlists" / "nse_all.csv"
HISTORY_YEARS = 2          # enough for the 200 EMA and the 250-bar history rule
KEEP_ROWS = 5              # only the latest rows are needed to scan "today"


class MarketScanError(Exception):
    """Raised when the NSE list or prices can't be obtained."""


# ---------------------------------------------------------------- the NSE list

def parse_equity_list(content: bytes, sectors: dict[str, str] | None = None) -> pd.DataFrame:
    """NSE's EQUITY_L.csv → symbol,name,sector for the normal 'EQ' series only."""
    raw = pd.read_csv(io.BytesIO(content), dtype=str)
    raw.columns = [c.strip().upper() for c in raw.columns]
    if not {"SYMBOL", "NAME OF COMPANY", "SERIES"} <= set(raw.columns):
        raise MarketScanError("Unexpected NSE equity list format.")
    raw = raw[raw["SERIES"].fillna("").str.strip() == "EQ"]
    df = pd.DataFrame({
        "symbol": raw["SYMBOL"].fillna("").str.strip().str.upper(),
        "name": raw["NAME OF COMPANY"].fillna("").str.strip(),
    })
    df = df[df["symbol"] != ""].drop_duplicates("symbol")
    df["sector"] = [(sectors or {}).get(s, "Unknown") for s in df["symbol"]]
    return df.sort_values("symbol").reset_index(drop=True)


def load_equity_list(cfg: Config, refresh: bool = False, path: Path = LIST_PATH,
                     timeout: int = 30) -> tuple[pd.DataFrame, str]:
    """The cached NSE list, downloaded when missing or ``refresh``. Returns (list, note)."""
    sectors: dict[str, str] = {}
    try:
        wl = load_watchlist(cfg.watchlist_path)
        sectors = dict(zip(wl["symbol"], wl["sector"]))
    except Exception:  # sectors are a nicety; the scan works without them
        pass
    if path.exists() and not refresh:
        df = pd.read_csv(path, dtype=str).fillna("")
        return df, f"NSE list: {len(df)} stocks (saved copy; use --refresh-list to update)"

    last_err: Exception | None = None
    for url in NSE_EQUITY_URLS:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                df = parse_equity_list(resp.read(), sectors)
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False)
            return df, f"NSE list: {len(df)} stocks (downloaded from NSE)"
        except Exception as err:
            last_err = err
            log.warning("NSE list download failed from %s: %s", url, err)
    if path.exists():
        df = pd.read_csv(path, dtype=str).fillna("")
        return df, f"NSE list: {len(df)} stocks (saved copy; download failed: {last_err})"
    raise MarketScanError(f"Could not download the NSE stock list ({last_err}). Check the internet connection.")


# ---------------------------------------------------------------- the scan

@dataclass
class NearMiss:
    """An affordable stock in an uptrend that fails only one of the remaining rules."""

    symbol: str
    name: str
    close: float
    missing: str
    max_shares: int


@dataclass
class MarketScan:
    """Result of a whole-market scan."""

    asof: date
    regime: RegimeState
    result: ScanResult
    capital: float
    cash: float
    what_if: bool
    listed: int
    with_data: int
    near_misses: list[NearMiss] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _all_cfg(cfg: Config) -> Config:
    return cfg.model_copy(update={"data": cfg.data.model_copy(update={"history_years": HISTORY_YEARS})})


def update_prices(conn: sqlite3.Connection, cfg: Config, symbols: list[str], source=None) -> str:
    """Download missing/new bars for every symbol (plus Nifty and VIX)."""
    if source is None:
        from .data_sources.yfinance_source import YFinanceSource

        source = YFinanceSource(batch_size=50, pause_seconds=1.0)
    wanted = [cfg.data.index_symbol, cfg.data.vix_symbol] + symbols
    rep = DataFetcher(conn, source, _all_cfg(cfg)).update(symbols=wanted)
    if rep.offline:
        return "All downloads failed (no internet?); using saved prices, which may be stale."
    return (f"Prices: {len(rep.full_downloads)} stocks downloaded in full, "
            f"{rep.total_new_bars} new bars, {len(rep.failed)} with no data (often new or suspended listings).")


def scan_market(
    cfg: Config,
    capital: float | None = None,
    download: bool = True,
    refresh_list: bool = False,
    all_conn: sqlite3.Connection | None = None,
    main_conn: sqlite3.Connection | None = None,
    source=None,
    progress=lambda msg: None,
    list_path: Path = LIST_PATH,
) -> MarketScan:
    """Run the entry rules on every NSE stock and size the signals.

    ``capital`` = what-if amount (all of it free cash); otherwise your saved capital and
    the cash left after your logged positions.
    """
    all_conn = all_conn or db.connect(ALL_DB_PATH)
    main_conn = main_conn or db.connect()
    listing, note = load_equity_list(cfg, refresh=refresh_list, path=list_path)
    notes = [note]
    progress(note)
    db.upsert_symbols(all_conn, listing)
    symbols = listing["symbol"].tolist()

    if download:
        progress(f"Downloading prices for {len(symbols)} stocks (the first time takes 10–20 minutes)…")
        msg = update_prices(all_conn, cfg, symbols, source)
        notes.append(msg)
        progress(msg)

    index_df = db.get_prices(all_conn, cfg.data.index_symbol)
    if index_df.empty:
        index_df = db.get_prices(main_conn, cfg.data.index_symbol)
    vix_df = db.get_prices(all_conn, cfg.data.vix_symbol)
    if vix_df.empty:
        vix_df = db.get_prices(main_conn, cfg.data.vix_symbol)
    if index_df.empty:
        raise MarketScanError("No Nifty prices yet. Run without --no-download first.")
    regime_series = compute_regime_series(index_df, vix_df, cfg)
    asof = index_df.index[-1].date()
    regime = regime_at(regime_series, asof, cfg)

    progress("Checking the rules on every stock…")
    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        px = db.get_prices(all_conn, sym)
        if len(px) < 2:
            continue
        frames[sym] = setup_frame(add_all_indicators(px, cfg, index_df["close"]), cfg).tail(KEEP_ROWS)

    trades = db.get_trades(main_conn)
    if capital is not None:
        cap, cash, held, sector_counts = float(capital), float(capital), set(), {}
    else:
        pf = build_portfolio(trades, cfg.account.capital)
        cap, cash, held = pf.capital, pf.available_cash, set(pf.positions)
        sector_counts = pf.sector_counts(dict(zip(listing["symbol"], listing["sector"])))

    result = scan(frames, regime.date, regime, cfg, listing,
                  results_lookup=results_dates_by_symbol(main_conn))
    result.signals = rank_and_select(result.signals, regime.max_positions, held, sector_counts,
                                     cfg.account.max_per_sector)
    size_signals(result.signals, cap, cash, cfg, regime.risk_multiplier)

    per_stock = cap * cfg.account.max_position_pct / 100
    names = dict(zip(listing["symbol"], listing["name"]))
    signal_syms = {s.symbol for s in result.signals}
    near: list[tuple[float, NearMiss]] = []
    ts = pd.Timestamp(regime.date)
    for sym, f in frames.items():
        if sym in signal_syms or ts not in f.index:
            continue
        row = f.loc[ts]
        if not (row["c_universe"] and row["c_uptrend"]):
            continue
        fails = [k for k in CONDITIONS[2:] if not bool(row[f"c_{k}"])]
        shares = math.floor(min(per_stock, cash) / float(row["close"]))
        if len(fails) == 1 and shares >= 1:
            rs = 0.0 if pd.isna(row.get("rs")) else float(row["rs"])
            near.append((rs, NearMiss(sym, names.get(sym, ""), float(row["close"]),
                                      CONDITION_LABELS[fails[0]], shares)))
    near.sort(key=lambda t: t[0], reverse=True)

    return MarketScan(asof=regime.date, regime=regime, result=result, capital=cap, cash=cash,
                      what_if=capital is not None, listed=len(symbols), with_data=len(frames),
                      near_misses=[n for _, n in near], notes=notes)


# ---------------------------------------------------------------- report

def format_market_scan(m: MarketScan, cfg: Config, max_near: int = 20) -> str:
    """Markdown summary of a whole-market scan."""
    from . import DISCLAIMER
    from .fmt import inr
    from .report import REGIME_ICON, signal_block

    r, res = m.regime, m.result
    per_stock = m.capital * cfg.account.max_position_pct / 100
    out = [f"# Whole-NSE scan — {m.asof}", ""]
    out.append(f"## Market: {REGIME_ICON.get(r.regime, '')} {r.regime}")
    out.append(r.description)
    out.append("")
    label = "WHAT-IF capital" if m.what_if else "Capital"
    out.append(f"{label} {inr(m.capital, 0)}  ·  Cash {inr(m.cash, 0)}  ·  "
               f"Max per stock {inr(per_stock, 0)} ({cfg.account.max_position_pct:g}%)  ·  "
               f"Risk per trade {inr(m.capital * cfg.account.risk_per_trade_pct / 100 * r.risk_multiplier, 0)}")
    out += [f"- {n}" for n in m.notes]
    out.append(f"- Scanned {res.scanned} of {m.listed} listed stocks"
               + (f"; {len(res.stale)} had no bar on {m.asof} (skipped)" if res.stale else ""))

    take = [s for s in res.signals if s.status == "TAKE"]
    watch = [s for s in res.signals if s.status == "WATCH"]
    out += ["", f"## Buy signals — TAKE ({len(take)})"]
    if not take:
        out.append("None today." if r.allows_entries else f"No new entries while the market is {r.regime}.")
    for s in take:
        out += [""] + signal_block(s)
    out += ["", f"## WATCH ({len(watch)})"]
    if not watch:
        out.append("None.")
    for s in watch:
        out += [""] + signal_block(s)

    out += ["", f"## Close to a setup and affordable ({len(m.near_misses)})",
            "In an uptrend and liquid, missing only one rule. Not a buy yet; check them again tomorrow."]
    if m.near_misses:
        out.append("")
        out.append("| Stock | Close | Shares you could hold | Still missing |")
        out.append("|---|---:|---:|---|")
        for n in m.near_misses[:max_near]:
            out.append(f"| {n.symbol} — {n.name} | {inr(n.close)} | {n.max_shares} | {n.missing} |")
        if len(m.near_misses) > max_near:
            out.append(f"| … {len(m.near_misses) - max_near} more | | | |")
    else:
        out.append("None.")

    out += ["", f"## Scan funnel ({res.scanned} stocks)"]
    for k in CONDITIONS:
        if res.funnel.get(k):
            out.append(f"- {res.funnel[k]}: {CONDITION_LABELS[k]}")
    out.append(f"- {res.funnel.get('passed', 0)}: passed all technical conditions")
    if res.rejected:
        out.append("- Rejected after passing:")
        out += [f"  - {sym}: {why}" for sym, why in res.rejected]
    out += ["", "---", f"_{DISCLAIMER}_"]
    return "\n".join(out)
