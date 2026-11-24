"""Swing Trading Agent: command-line entry point.

Educational tool for personal use. Not investment advice.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from swing_agent import DISCLAIMER  # noqa: E402
from swing_agent.config import ConfigError, load_config  # noqa: E402
from swing_agent.db import DatabaseError  # noqa: E402
from swing_agent.logging_setup import setup_logging  # noqa: E402
from swing_agent.pipeline import NoDataError  # noqa: E402
from swing_agent.portfolio import PortfolioError  # noqa: E402
from swing_agent.trading import TradeError  # noqa: E402
from swing_agent.watchlist import WatchlistError  # noqa: E402

log = logging.getLogger("swing")


# ---------------------------------------------------------------- output helpers

def _print_update_report(report, title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"Latest trading day (Nifty): {report.latest_trading_day or 'unknown'}")
    print(f"Symbols updated: {len(report.new_bars)}   New bars: {report.total_new_bars}")
    if report.full_downloads:
        print(f"Full history downloaded: {len(report.full_downloads)} symbol(s)")
    if report.readjusted:
        print(f"Re-downloaded after corporate action: {', '.join(report.readjusted)}")
    if report.offline:
        print("\n!! WARNING: every download failed (no internet?). Using cached data, which may be STALE.")
    if report.failed:
        print(f"\nFailed downloads ({len(report.failed)}), skipped today:")
        for sym, why in sorted(report.failed.items()):
            print(f"  - {sym}: {why}")
    other = [i for i in report.issues if i.kind != "FAILED"]
    if other:
        print(f"\nData warnings ({len(other)}):")
        for i in other[:40]:
            print(f"  - {i.message}")
        if len(other) > 40:
            print(f"  ... and {len(other) - 40} more (see logs/swing.log)")
    print(f"\n{DISCLAIMER}")


def _fetcher(cfg, conn):
    from swing_agent.data_fetcher import DataFetcher
    from swing_agent.data_sources import get_source

    return DataFetcher(conn, get_source(cfg.data.source), cfg)


# ---------------------------------------------------------------- commands

def cmd_init(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.watchlist import load_watchlist

    wl = load_watchlist(cfg.watchlist_path)
    conn = db.connect()
    db.upsert_symbols(conn, wl)
    print(f"Database ready at {db.DEFAULT_DB_PATH}")
    print(f"Watchlist loaded: {len(wl)} symbols from {cfg.watchlist_path}")
    print(f"Downloading ~{cfg.data.history_years} years of daily data for the watchlist + "
          f"{cfg.data.index_symbol} + {cfg.data.vix_symbol}. This takes a few minutes...")
    report = _fetcher(cfg, conn).update(full=args.full)
    _print_update_report(report, "Initial download")
    return 1 if report.offline else 0


def cmd_update(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.watchlist import load_watchlist

    conn = db.connect()
    db.upsert_symbols(conn, load_watchlist(cfg.watchlist_path))
    report = _fetcher(cfg, conn).update(full=args.full)
    _print_update_report(report, "Data update")
    return 1 if report.offline else 0


def cmd_watchlist(args, cfg) -> int:
    from swing_agent.watchlist import load_watchlist, refresh_watchlist

    if args.refresh:
        df = refresh_watchlist(cfg.watchlist_path)
        print(f"Watchlist refreshed: {len(df)} symbols saved to {cfg.watchlist_path}")
        print("Run `python swing.py update` to download data for any new symbols.")
    else:
        df = load_watchlist(cfg.watchlist_path)
        print(df.to_string(index=False))
        print(f"\n{len(df)} symbols in {cfg.watchlist_path}")
    return 0


def _parse_date(text: str | None, flag: str = "--date"):
    from datetime import date

    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"Error: {flag} must look like 2024-03-15 (got {text!r}).")


def cmd_scan(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.pipeline import daily
    from swing_agent.report import format_daily

    conn = db.connect()
    result = daily(conn, cfg, do_update=False, persist=False, asof=_parse_date(args.date))
    print(format_daily(result, cfg))
    return 0


def cmd_run(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.pipeline import daily
    from swing_agent.report import format_daily

    conn = db.connect()
    result = daily(conn, cfg, do_update=not args.no_update, persist=True)
    print(format_daily(result, cfg))
    print(f"\nReport saved to {result.report_path}")
    return 1 if result.update and result.update.offline else 0


def cmd_trade(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.fmt import inr
    from swing_agent.portfolio import build_portfolio
    from swing_agent.trading import log_buy, log_sell, move_stop

    conn = db.connect()
    if args.action == "buy":
        res = log_buy(conn, cfg, args.symbol, args.price, args.qty, _parse_date(args.date),
                      stop=args.stop, target=args.target, fees=args.fees, note=args.note)
        print(f"Logged BUY #{res.trade_id}: {args.qty} {args.symbol.upper()} @ {inr(args.price)}")
    elif args.action == "sell":
        res = log_sell(conn, cfg, args.symbol, args.price, args.qty, _parse_date(args.date),
                       fees=args.fees, note=args.note)
        print(f"Logged SELL #{res.trade_id}: {args.qty} {args.symbol.upper()} @ {inr(args.price)}")
    elif args.action == "move-stop":
        for m in move_stop(conn, cfg, args.symbol, args.stop):
            print(m)
        return 0
    elif args.action == "delete":
        ok = db.delete_trade(conn, args.trade_id)
        print(f"Deleted trade #{args.trade_id}." if ok else f"No trade #{args.trade_id}.")
        return 0 if ok else 1
    else:  # list
        trades = db.get_trades(conn)
        pf = build_portfolio(trades, cfg.account.capital)
        cols = ["id", "date", "symbol", "side", "qty", "price", "stop", "target", "fees", "signal_id"]
        if trades.empty:
            print("No trades logged yet.")
        else:
            view = trades[cols].copy()
            for c in ("price", "stop", "target", "fees"):
                view[c] = view[c].map(lambda x: "" if x != x else f"{x:,.2f}")
            view["signal_id"] = view["signal_id"].map(lambda x: "" if x != x else int(x))
            print(view.to_string(index=False))
        print(f"\nOpen positions: {len(pf.positions)}   Capital {inr(pf.capital, 0)}   "
              f"Cash {inr(pf.available_cash, 0)}   Realised P&L {inr(pf.realised_pnl, 0)}")
        for p in pf.positions.values():
            print(f"  {p.symbol}: {p.qty} @ {inr(p.avg_entry)}  stop {inr(p.stop)}  target {inr(p.target)}")
        return 0
    for m in res.messages:
        print("  " + m)
    return 0


def cmd_backtest(args, cfg) -> int:
    from datetime import datetime

    from swing_agent import db
    from swing_agent.backtest import (
        backtest_from_cache,
        load_prices,
        parse_sweep,
        run_sweep,
        summary_markdown,
        write_outputs,
    )
    from swing_agent.config import PROJECT_ROOT

    start, end = _parse_date(args.start, "--start"), _parse_date(args.end, "--end")
    conn = db.connect()
    try:
        cache = load_prices(conn, cfg)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if args.sweep:
            grid = parse_sweep(args.sweep, cfg)
            n = 1
            for _, _, vals in grid:
                n *= len(vals)
            print(f"Running {n} backtests (this checks stability, not which one is 'best')...")
            table = run_sweep(cache, cfg, grid, start, end)
            out = PROJECT_ROOT / "reports" / f"sweep_{stamp}.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            table.to_csv(out, index=False)
            print(table.round(2).to_string(index=False))
            print(f"\nSaved to {out}")
            print("Look for results that stay similar across nearby values. Judge on the oos_ columns.")
            return 0
        print("Running backtest...")
        res = backtest_from_cache(cache, cfg, start, end)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    out = PROJECT_ROOT / "reports" / f"backtest_{stamp}"
    periods = write_outputs(res, cfg, out)
    print(summary_markdown(res, cfg, periods))
    print(f"\nSaved trades.csv, equity.csv and summary.md to {out}")
    return 0


def _not_yet(milestone: int):
    def _cmd(args, cfg) -> int:
        print(f"`{args.command}` is not built yet (planned for milestone {milestone}).")
        return 2

    return _cmd


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swing.py", description="Swing Trading Agent (NSE). " + DISCLAIMER)
    p.add_argument("--config", help="Path to config.yaml (default: project config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show info logs on the console")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="Create DB, load watchlist, first full download")
    s.add_argument("--full", action="store_true", help="Re-download full history even if data exists")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("update", help="Incremental data update")
    s.add_argument("--full", action="store_true", help="Re-download full history for every symbol")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("watchlist", help="Show or refresh the watchlist")
    s.add_argument("--refresh", action="store_true", help="Download latest Nifty 100 list from niftyindices.com")
    s.set_defaults(func=cmd_watchlist)

    s = sub.add_parser("scan", help="Regime + signals + exits, print report (no download, no save)")
    s.add_argument("--date", help="Scan as of this past date (YYYY-MM-DD); default latest")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("run", help="update + scan + save report (used by the scheduler)")
    s.add_argument("--no-update", action="store_true", help="Skip the download step")
    s.set_defaults(func=cmd_run)

    t = sub.add_parser("trade", help="Log your own trades (no broker connection)")
    tsub = t.add_subparsers(dest="action", required=True)
    for side in ("buy", "sell"):
        s = tsub.add_parser(side, help=f"Log a {side}")
        s.add_argument("symbol")
        s.add_argument("--price", type=float, required=True)
        s.add_argument("--qty", type=int, required=True)
        s.add_argument("--date", help="Trade date YYYY-MM-DD (default today)")
        s.add_argument("--fees", type=float, help="Actual charges (default: estimated)")
        s.add_argument("--note")
        if side == "buy":
            s.add_argument("--stop", type=float, help="Stop-loss (default: from latest signal)")
            s.add_argument("--target", type=float, help="Target (default: target_r × R from your fill)")
    s = tsub.add_parser("move-stop", help="Change the stop of an open position")
    s.add_argument("symbol")
    s.add_argument("--stop", type=float, required=True)
    s = tsub.add_parser("delete", help="Delete a mistaken trade row")
    s.add_argument("trade_id", type=int)
    tsub.add_parser("list", help="Show trades and open positions")
    t.set_defaults(func=cmd_trade)

    s = sub.add_parser("backtest", help="Run the backtester on stored history")
    s.add_argument("--start", help="Start date YYYY-MM-DD (default: config backtest.start_date)")
    s.add_argument("--end", help="End date YYYY-MM-DD (default: latest data)")
    s.add_argument("--sweep", nargs="+", metavar="KEY=V1,V2",
                   help="Parameter grid, e.g. --sweep target_r=1.5,2,2.5 rsi_dip_max=45,50,55")
    s.set_defaults(func=cmd_backtest)

    for name, ms, helptext in [
        ("dashboard", 5, "Launch the Streamlit dashboard"),
        ("events", 6, "Manage the event calendar"),
    ]:
        s = sub.add_parser(name, help=f"{helptext} (milestone {ms})")
        s.set_defaults(func=_not_yet(ms))
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # ₹ and emoji on Windows consoles
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    setup_logging(console_level=logging.INFO if args.verbose else logging.ERROR)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (ConfigError, WatchlistError, DatabaseError, NoDataError, TradeError, PortfolioError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
