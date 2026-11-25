"""Analyse any NSE stock on demand: rule checklist, trade plan and an approximate target date.

The verdict always comes from the same scanner rules as the daily scan. When the stock is not
a setup today, a clearly labelled *hypothetical* plan ("if you bought at today's close") is
shown for information; it is not a signal.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import db
from .config import PROJECT_ROOT, Config
from .costs import round_trip_costs
from .events import check_results, refresh_earnings, results_dates_by_symbol
from .fmt import inr
from .indicators import add_all_indicators
from .market_filter import RegimeState, compute_regime_series, regime_at
from .portfolio import build_portfolio
from .risk import position_size, sizing_steps
from .scanner import Signal, evaluate_row, plan_trade, setup_frame

log = logging.getLogger(__name__)


class AnalyzeError(Exception):
    """Raised when a symbol can't be analysed (unknown symbol, no data, ...)."""


# ---------------------------------------------------------------- target-date estimate

@dataclass
class TargetHistory:
    """How long backtest trades took to reach their target."""

    p25: float
    median: float
    p75: float
    hit_rate_pct: float
    trades: int
    source: str


def target_history(reports_dir: Path | None = None) -> TargetHistory | None:
    """Stats from the newest saved backtest (reports/backtest_*/trades.csv), or None."""
    reports_dir = reports_dir or PROJECT_ROOT / "reports"
    folders = sorted((p for p in reports_dir.glob("backtest_*") if (p / "trades.csv").exists()), reverse=True)
    if not folders:
        return None
    try:
        tr = pd.read_csv(folders[0] / "trades.csv")
    except (OSError, pd.errors.ParserError):
        return None
    hits = tr[tr["exit_reason"] == "TARGET_HIT"]
    if len(tr) == 0 or len(hits) < 5:
        return None
    q = hits["bars_held"].quantile([0.25, 0.5, 0.75])
    return TargetHistory(float(q[0.25]), float(q[0.5]), float(q[0.75]),
                         len(hits) / len(tr) * 100, len(tr), folders[0].name)


@dataclass
class TargetETA:
    """Approximate window in which the target could be reached."""

    entry_date: date
    fastest_days: int | None          # distance / ATR: if it rose one full daily range every day
    earliest: date
    likely: date
    latest: date
    time_stop: date
    history: TargetHistory | None
    beyond_time_stop: bool

    def short(self) -> str:
        """One line for reports: 'about 2 Dec – 12 Dec 2026 (likely ~5 Dec); time stop 22 Dec'."""
        f = lambda d: d.strftime("%d %b")  # noqa: E731
        return (f"about {f(self.earliest)} – {self.latest.strftime('%d %b %Y')} (most likely ~{f(self.likely)}); "
                f"time stop {f(self.time_stop)}"
                + ("; only ~{:.0f}% of backtest trades reached target".format(self.history.hit_rate_pct)
                   if self.history else ""))

    def text(self, target: float) -> str:
        """One-paragraph explanation."""
        f = lambda d: d.strftime("%d %b %Y")  # noqa: E731
        parts = [f"Target {inr(target)}: roughly {f(self.earliest)} – {f(self.latest)}, "
                 f"most likely around {f(self.likely)} (counting from entry on {f(self.entry_date)})."]
        if self.fastest_days:
            parts.append(f"Fastest realistic: about {self.fastest_days} trading day(s) at this stock's "
                         "typical daily range.")
        if self.history:
            h = self.history
            parts.append(f"In the backtest, trades that hit target took {h.p25:.0f}–{h.p75:.0f} trading days "
                         f"(median {h.median:.0f}), but only {h.hit_rate_pct:.0f}% of trades reached target at all.")
        if self.beyond_time_stop:
            parts.append("The target is far for this stock's daily range: it may not be reached before the "
                         "time stop.")
        parts.append(f"Time stop (exit if not working): {f(self.time_stop)}. This is an estimate, not a promise.")
        return " ".join(parts)


def _bday(d: date, n: int) -> date:
    return pd.Timestamp(np.busday_offset(np.datetime64(d), int(n), roll="forward")).date()


def estimate_target(entry: float, target: float, atr: float | None, asof: date, cfg: Config,
                    history: TargetHistory | None) -> TargetETA:
    """Target-date window from the stock's ATR speed and backtest history (weekdays; no holiday calendar)."""
    entry_date = _bday(asof, 1)
    limit = cfg.stops_targets.time_stop_days
    fastest = max(1, math.ceil((target - entry) / atr)) if atr and atr > 0 and target > entry else None
    if history:
        lo, mid, hi = history.p25, history.median, history.p75
    else:  # no backtest yet: ATR-only, assume progress at ~half the daily range
        base = fastest or limit // 2
        lo, mid, hi = base, base * 2, base * 3
    if fastest:
        lo, mid, hi = max(lo, fastest), max(mid, fastest), max(hi, fastest)
    lo, mid, hi = (min(int(round(x)), limit) for x in (lo, mid, hi))
    return TargetETA(entry_date, fastest, _bday(entry_date, lo), _bday(entry_date, mid), _bday(entry_date, hi),
                     _bday(entry_date, limit), history, bool(fastest and fastest > limit))


# ---------------------------------------------------------------- analysis

@dataclass
class Check:
    """One rule with its result and the numbers behind it."""

    rule: str
    passed: bool
    detail: str


@dataclass
class Plan:
    """A trade plan (real signal or hypothetical)."""

    kind: str                    # "SIGNAL" or "HYPOTHETICAL"
    entry: float
    stop: float | None
    target: float | None
    r: float | None
    stop_pct: float | None
    qty: int
    risk_amount: float
    est_costs: float | None
    eta: TargetETA | None
    note: str = ""
    buy_zone: tuple[float, float] | None = None
    qty_math: list[str] = field(default_factory=list)   # how the quantity was worked out


@dataclass
class Analysis:
    """Everything shown for one analysed symbol."""

    symbol: str
    name: str
    sector: str
    asof: date
    close: float
    regime: RegimeState
    checks: list[Check]
    is_setup: bool
    plan: Plan | None
    signal: Signal | None
    in_watchlist: bool
    downloaded: bool
    held: str | None = None
    results: str = "results date unknown"
    warnings: list[str] = field(default_factory=list)
    frame: pd.DataFrame | None = None
    sizing: str = ""


EXCHANGES = ("NSE", "BSE")


def clean_symbol(text: str, exchange: str = "NSE") -> str:
    """Normalise user input to the stored symbol key.

    NSE: 'tcs.ns ' → 'TCS'. BSE: 'tcs' → 'TCS.BO' (Yahoo uses the trading symbol, not the scrip code).
    A typed '.NS' / '.BO' suffix wins over the ``exchange`` choice.
    """
    s = text.strip().upper()
    exchange = exchange.strip().upper()
    if exchange not in EXCHANGES:
        raise AnalyzeError(f"Exchange must be NSE or BSE (got {exchange!r}).")
    if s.endswith(".NS"):
        s, exchange = s[:-3], "NSE"
    elif s.endswith(".BO"):
        s, exchange = s[:-3], "BSE"
    if not s or not all(c.isalnum() or c in "&-_" for c in s):
        raise AnalyzeError(f"{text!r} doesn't look like a stock symbol (e.g. RELIANCE, TCS, M&M).")
    return f"{s}.BO" if exchange == "BSE" else s


def exchange_of(symbol: str) -> str:
    """'TCS.BO' → 'BSE', anything else → 'NSE'."""
    return "BSE" if symbol.upper().endswith(".BO") else "NSE"


def _ensure_prices(conn: sqlite3.Connection, cfg: Config, sym: str, allow_download: bool, source) -> bool:
    """Make sure the symbol has up-to-date prices; download if needed and allowed. Returns True if downloaded."""
    from .data_fetcher import DataFetcher
    from .data_sources import get_source

    latest_index = db.last_price_date(conn, cfg.data.index_symbol)
    last = db.last_price_date(conn, sym)
    if last is not None and (latest_index is None or last >= latest_index):
        return False
    if not allow_download:
        if last is None:
            raise AnalyzeError(f"No stored prices for {sym}. Allow downloading to analyse it.")
        return False
    rep = DataFetcher(conn, source or get_source(cfg.data.source), cfg).update(symbols=[sym])
    if sym not in rep.failed:
        _remember_symbol(conn, sym, source)
    if sym in rep.failed and db.last_price_date(conn, sym) is None:
        raise AnalyzeError(f"Couldn't get prices for {sym} from Yahoo ({rep.failed[sym]}). Check the symbol and "
                           "exchange (use the trading symbol, e.g. RELIANCE, even on BSE; numeric BSE codes don't "
                           "work; TATAMOTORS is now TMPV) or your internet connection.")
    if cfg.data.fetch_results_dates and sym not in results_dates_by_symbol(conn):
        refresh_earnings(conn, [sym], force=True, update_marker=False)
    return True


def _remember_symbol(conn: sqlite3.Connection, sym: str, source) -> None:
    """Store name/sector for a symbol outside the watchlist (inactive, so the daily scan skips it)."""
    if conn.execute("SELECT 1 FROM symbols WHERE symbol=?", (sym,)).fetchone():
        return
    name, sector = sym, "Unknown"
    if source is None:  # real Yahoo only; best effort
        try:
            import yfinance as yf

            from .data_sources.yfinance_source import to_yf_symbol

            info = yf.Ticker(to_yf_symbol(sym)).info or {}
            name = info.get("longName") or info.get("shortName") or sym
            sector = info.get("sector") or "Unknown"
        except Exception as err:  # noqa: BLE001
            log.info("No company info for %s: %s", sym, err)
    conn.execute("INSERT OR IGNORE INTO symbols(symbol, name, sector, active) VALUES (?, ?, ?, 0)",
                 (sym, name, sector))
    conn.commit()


def _checks(row: pd.Series, cfg: Config, regime: RegimeState, results_txt: str, results_ok: bool) -> list[Check]:
    e, u = cfg.entry, cfg.universe
    v = lambda k: float(row[k]) if pd.notna(row[k]) else float("nan")  # noqa: E731
    ema_l_then = v("ema_l") if pd.isna(row.get("ema_l_prev")) else v("ema_l_prev")
    ok = lambda cond: "✓" if cond else "✗"  # noqa: E731  (marks each part of a multi-part rule)
    close, ema_s, ema_m, ema_l = v("close"), v("ema_s"), v("ema_m"), v("ema_l")
    tol = 1 + e.touch_tolerance_pct / 100
    return [
        Check("Price & liquidity", bool(row["c_universe"]),
              f"{ok(close >= u.min_price)} close {inr(close)} (min {inr(u.min_price, 0)}); "
              f"{ok(v('atv_cr') >= u.min_avg_traded_value_cr)} avg traded value ₹{v('atv_cr'):.0f} cr "
              f"(min ₹{u.min_avg_traded_value_cr:g} cr); "
              f"{ok(row['bars'] >= u.min_history_bars)} {int(row['bars'])} days of history (min {u.min_history_bars})"),
        Check("Uptrend", bool(row["c_uptrend"]),
              f"{ok(close > ema_l)} close {inr(close)} above {e.ema_long} EMA {inr(ema_l)}; "
              f"{ok(ema_m > ema_l)} {e.ema_mid} EMA {inr(ema_m)} above {e.ema_long} EMA; "
              f"{ok(ema_l > ema_l_then)} {e.ema_long} EMA rising (was {inr(ema_l_then)} "
              f"{e.ema_long_slope_lookback} days ago)"),
        Check(f"Pullback {e.pullback_min_pct:g}–{e.pullback_max_pct:g}%", bool(row["c_pullback"]),
              f"{v('pullback_pct'):.1f}% below the {e.pullback_lookback}-day high {inr(v('high_lb'))}"),
        Check("Touched support", bool(row["c_touch"]),
              f"{ok(v('touch_low') <= ema_s * tol)} recent low {inr(v('touch_low'))} reached the "
              f"{e.ema_short} EMA {inr(ema_s)}; {ok(close > ema_m)} close above {e.ema_mid} EMA {inr(ema_m)}"),
        Check("RSI reset & turning up", bool(row["c_rsi"]),
              f"{ok(e.rsi_dip_min <= v('rsi_min') <= e.rsi_dip_max)} lowest RSI in {e.rsi_dip_lookback} days "
              f"{v('rsi_min'):.0f} (needs {e.rsi_dip_min:g}–{e.rsi_dip_max:g}); "
              f"{ok(v('rsi') > v('rsi_prev'))} RSI today {v('rsi'):.0f} above yesterday's {v('rsi_prev'):.0f}"),
        Check("Bullish trigger", bool(row["c_trigger"]),
              f"{ok(close > v('prev_high'))} close {inr(close)} above yesterday's high {inr(v('prev_high'))}; "
              f"{ok(close > ema_s)} above {e.ema_short} EMA; {ok(close > v('open'))} above today's open "
              f"{inr(v('open'))}"),
        Check("No results soon", results_ok, results_txt),
        Check("Market allows new buys", regime.allows_entries, f"market is {regime.regime}"),
    ]


def analyze_symbol(
    conn: sqlite3.Connection,
    cfg: Config,
    symbol: str,
    allow_download: bool = True,
    source=None,
    reports_dir: Path | None = None,
    capital_override: float | None = None,
    exchange: str = "NSE",
) -> Analysis:
    """Run the full rule checklist and plan for one symbol on the latest trading day.

    ``capital_override`` sizes the plan as if your capital were that amount ("what if"), without
    changing the saved capital. Money already in your open positions still reduces the cash.
    """
    if capital_override is not None and capital_override <= 0:
        raise AnalyzeError("The what-if capital must be more than ₹0.")
    sym = clean_symbol(symbol, exchange)
    index = db.get_prices(conn, cfg.data.index_symbol)
    if index.empty:
        raise AnalyzeError("No Nifty data yet. Run `python swing.py init` first.")
    downloaded = _ensure_prices(conn, cfg, sym, allow_download, source)
    px = db.get_prices(conn, sym)
    if px.empty:
        raise AnalyzeError(f"No price data for {sym}.")

    frame = setup_frame(add_all_indicators(px, cfg, index["close"]), cfg)
    frame["ema_l_prev"] = frame["ema_l"].shift(cfg.entry.ema_long_slope_lookback)
    row = frame.iloc[-1]
    asof = frame.index[-1].date()
    regime = regime_at(compute_regime_series(index, db.get_prices(conn, cfg.data.vix_symbol), cfg), asof, cfg)

    meta = db.get_symbols(conn, active_only=False).set_index("symbol")
    in_wl = sym in set(db.get_symbols(conn)["symbol"])
    name = str(meta.at[sym, "name"]) if sym in meta.index else ""
    sector = str(meta.at[sym, "sector"]) if sym in meta.index else "Unknown"

    rc = check_results(results_dates_by_symbol(conn).get(sym), asof)
    if rc.unknown:
        results_txt, results_ok = "results date unknown (not blocking)", True
    else:
        results_ok = not rc.within(cfg.entry.avoid_results_within_days)
        results_txt = (f"results on {rc.next_date} ({rc.trading_days_away} trading days away; "
                       f"blocks within {cfg.entry.avoid_results_within_days})")

    checks = _checks(row, cfg, regime, results_txt, results_ok)
    pf = build_portfolio(db.get_trades(conn), cfg.account.capital)
    capital, cash = pf.capital, pf.available_cash
    if capital_override is not None:
        capital = float(capital_override)
        cash = capital   # what-if = "if I had this much to trade with", all of it free
    hist = target_history(reports_dir)
    warnings: list[str] = []
    if asof < index.index[-1].date():
        warnings.append(f"Latest price for {sym} is from {asof}, older than Nifty's {index.index[-1].date()}.")

    sig, why = evaluate_row(sym, row, asof, cfg, name, sector, results_dates=results_dates_by_symbol(conn).get(sym))
    is_setup = sig is not None and regime.allows_entries
    mult = regime.risk_multiplier if regime.allows_entries else 1.0

    if is_setup:
        size = position_size(sig.entry_plan, sig.stop, capital, cash, cfg, mult)
        sig.qty, sig.risk_amount = size.qty, size.risk_amount
        plan = Plan("SIGNAL", sig.entry_plan, sig.stop, sig.target, sig.r, sig.stop_pct, size.qty, size.risk_amount,
                    round_trip_costs(sig.entry_plan, sig.target, size.qty, cfg.costs) if size.qty else None,
                    estimate_target(sig.entry_plan, sig.target, sig.atr, asof, cfg, hist),
                    note=size.reason or "", buy_zone=(sig.buy_zone_low, sig.buy_zone_high),
                    qty_math=sizing_steps(sig.entry_plan, sig.stop, capital, cash, cfg, mult, regime.regime))
        warnings += sig.warnings
    else:
        entry = float(row["close"])
        tp, reject = plan_trade(entry, float(row["swing_low"]), float(row["atr"]), cfg)
        if tp is None:
            plan = Plan("HYPOTHETICAL", entry, None, None, None, None, 0, 0.0, None, None,
                        note=f"No sensible plan at today's close: {reject}.")
        else:
            size = position_size(entry, tp.stop, capital, cash, cfg, mult)
            plan = Plan("HYPOTHETICAL", entry, tp.stop, tp.target, tp.r, tp.stop_pct, size.qty, size.risk_amount,
                        round_trip_costs(entry, tp.target, size.qty, cfg.costs) if size.qty else None,
                        estimate_target(entry, tp.target, float(row["atr"]), asof, cfg, hist),
                        note=size.reason or "",
                        qty_math=sizing_steps(entry, tp.stop, capital, cash, cfg, mult, regime.regime))
        if sig is None and bool(row["setup"]) and why:  # passed the chart rules but was rejected
            warnings.append(f"Rules rejected it: {why}.")
        if not regime.allows_entries:
            warnings.append(f"Market is {regime.regime}: the rules allow no new buys at all right now.")

    held = None
    if sym in pf.positions:
        p = pf.positions[sym]
        held = f"You hold {p.qty} @ {inr(p.avg_entry)} (stop {inr(p.stop)}, target {inr(p.target)})."
    if plan is not None and plan.est_costs and plan.qty and plan.est_costs >= plan.risk_amount:
        warnings.append(f"Charges (~{inr(plan.est_costs)} round trip) are as big as or bigger than the "
                        f"{inr(plan.risk_amount)} at risk: at this capital the trade can't pay off sensibly.")
    a = cfg.account
    limits = (f"(available cash {inr(cash, 0)}): risk up to {inr(capital * a.risk_per_trade_pct / 100 * mult, 0)} "
              f"per trade, max {inr(capital * a.max_position_pct / 100, 0)} in one stock.")
    if capital_override is not None:
        sizing = (f"WHAT-IF: sized as if you had {inr(capital, 0)} to trade with, all of it free {limits} "
                  f"Your saved capital ({inr(pf.capital, 0)}) is unchanged.")
    else:
        sizing = (f"Sized for your capital {inr(capital, 0)} {limits} "
                  "Change it with `python swing.py capital <amount>` or on the dashboard's Settings page.")
    return Analysis(sym, name, sector, asof, float(row["close"]), regime, checks, is_setup, plan,
                    sig if is_setup else None, in_wl, downloaded, held, rc_text(rc), warnings, frame, sizing)


def rc_text(rc) -> str:
    """Short results-date line."""
    return "results date unknown" if rc.unknown else f"next results {rc.next_date} ({rc.trading_days_away} trading days)"


def add_to_watchlist(conn: sqlite3.Connection, cfg: Config, symbol: str, name: str = "", sector: str = "") -> bool:
    """Append a symbol to the watchlist CSV and activate it in the DB. Returns False if already there."""
    from .watchlist import load_watchlist

    sym = clean_symbol(symbol)
    path = cfg.watchlist_path
    wl = load_watchlist(path)
    if sym in set(wl["symbol"]):
        return False
    wl = pd.concat([wl, pd.DataFrame([{"symbol": sym, "name": name or sym, "sector": sector or "Unknown"}])],
                   ignore_index=True)
    wl.to_csv(path, index=False)
    db.upsert_symbols(conn, wl)
    return True


def format_analysis(a: Analysis) -> str:
    """Markdown/terminal text for an analysis."""
    title = (f"# {a.symbol} [{exchange_of(a.symbol)}]" + (f" — {a.name}" if a.name else "")
             + f" ({a.sector})")
    out = [title, f"Data as of {a.asof} · close {inr(a.close)} · market {a.regime.regime}"
           + (" · downloaded just now" if a.downloaded else ""), ""]
    if a.held:
        out += [f"**{a.held}**", ""]
    verdict = ("✅ BUY SETUP by the rules (same as a daily-scan signal)" if a.is_setup
               else "❌ No buy setup today by the rules")
    out += [f"## Verdict: {verdict}", "", "## Rule checklist"]
    out += [f"- {'✅' if c.passed else '❌'} **{c.rule}**: {c.detail}" for c in a.checks]
    p = a.plan
    out += [""]
    if p is not None:
        if p.kind == "SIGNAL":
            out.append("## Trade plan (rules)")
            out.append(f"- Buy zone: {inr(p.buy_zone[0])} – {inr(p.buy_zone[1])} on the next trading day only; "
                       "skip if it opens above.")
        else:
            out.append("## Hypothetical plan: IF you bought at today's close (not a signal)")
            out.append(f"- Entry: {inr(p.entry)}")
        if p.stop is not None:
            out.append(f"- Stop-loss: {inr(p.stop)} ({p.stop_pct:.1f}% below entry) · Target: {inr(p.target)} "
                       f"(2R) · R per share {inr(p.r)}")
            out.append(f"- Quantity: {p.qty} · money needed {inr(p.qty * p.entry, 0)}"
                       f" · rupees at risk {inr(p.risk_amount)}"
                       + (f" · est. round-trip charges {inr(p.est_costs)}" if p.est_costs else "")
                       + (f" · {p.note}" if p.note else ""))
            if p.eta:
                out += ["", "## Approximate target date", p.eta.text(p.target)]
        else:
            out.append(f"- {p.note}")
    if a.sizing:
        out += ["", f"_{a.sizing}_"]
    if p is not None and p.qty_math:
        out += ["", "How the quantity was worked out:"] + [f"  {line}" for line in p.qty_math]
    if a.warnings:
        out += ["", "## Warnings"] + [f"- {w}" for w in a.warnings]
    if not a.in_watchlist:
        out += ["", f"_{a.symbol} is not in your watchlist, so the daily scan doesn't check it. "
                    "Add it with `python swing.py analyze " + a.symbol + " --add`._"]
    out += ["", "_Educational tool for personal use. Not investment advice._"]
    return "\n".join(out)
