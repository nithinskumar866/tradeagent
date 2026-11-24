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
from swing_agent.events import EventError  # noqa: E402
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

    if args.log_file:  # scheduled, windowless runs: write everything (including errors) to a file
        import traceback
        from datetime import datetime

        from swing_agent.config import PROJECT_ROOT

        path = Path(args.log_file)
        path = path if path.is_absolute() else PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
            try:
                result = daily(db.connect(), cfg, do_update=not args.no_update, persist=True)
                out.write(format_daily(result, cfg) + f"\n\nReport saved to {result.report_path}\n")
                return 1 if result.update and result.update.offline else 0
            except Exception:
                log.exception("Scheduled run failed")
                out.write("RUN FAILED:\n" + traceback.format_exc())
                return 1

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


def cmd_capital(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.config import set_capital
    from swing_agent.fmt import inr
    from swing_agent.portfolio import build_portfolio

    if args.amount is not None:
        cfg = set_capital(args.amount, args.config)
        print(f"Starting capital set to {inr(cfg.account.capital, 0)} in config.yaml.")
    pf = build_portfolio(db.get_trades(db.connect()), cfg.account.capital)
    a = cfg.account
    print(f"Starting capital {inr(a.capital, 0)} + realised P&L {inr(pf.realised_pnl, 0)} "
          f"= capital {inr(pf.capital, 0)}")
    print(f"Invested {inr(pf.invested, 0)} · available cash {inr(pf.available_cash, 0)}")
    print(f"Per trade: risk up to {inr(pf.capital * a.risk_per_trade_pct / 100, 0)} "
          f"({a.risk_per_trade_pct:g}%; half in a CAUTION market), max {inr(pf.capital * a.max_position_pct / 100, 0)} "
          f"in one stock ({a.max_position_pct:g}%), up to {a.max_open_positions} positions.")
    return 0


def cmd_analyze(args, cfg) -> int:
    from swing_agent import db
    from swing_agent.analyze import AnalyzeError, add_to_watchlist, analyze_symbol, format_analysis

    conn = db.connect()
    try:
        a = analyze_symbol(conn, cfg, args.symbol, allow_download=not args.no_download,
                           capital_override=args.capital, exchange=args.exchange)
    except AnalyzeError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    print(format_analysis(a))
    if args.add:
        if add_to_watchlist(conn, cfg, a.symbol, a.name, a.sector):
            print(f"\nAdded {a.symbol} to {cfg.universe.watchlist_file}; the daily scan will now include it.")
        else:
            print(f"\n{a.symbol} is already in the watchlist.")
    return 0


def cmd_dashboard(args, cfg) -> int:
    import subprocess

    app = Path(__file__).resolve().parent / "app.py"
    # Private by default: only this computer can connect. --lan opens it to the local network.
    address = "0.0.0.0" if args.lan else "localhost"
    print(f"Opening the dashboard at http://localhost:{args.port}  (Ctrl+C to stop)")
    if args.lan:
        ips = _lan_ips()
        print("Also reachable from phones/PCs on the same network at: "
              + (", ".join(f"http://{ip}:{args.port}" for ip in ips) or "(no LAN address found)"))
        print("Note: the dashboard has no login. Anyone on this network can see and change your trade log.")
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(args.port),
           "--server.address", address, "--browser.gatherUsageStats", "false"]
    if args.headless:
        cmd += ["--server.headless", "true"]
    return subprocess.call(cmd)


def _lan_ips() -> list[str]:
    """Private IPv4 addresses of this machine (192.168.x, 10.x, 172.16–31.x), best effort."""
    import ipaddress
    import socket

    primary = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # no packet is sent; just picks the default interface
            primary = s.getsockname()[0]
    except OSError:
        pass
    if primary and ipaddress.ip_address(primary).is_private and not primary.startswith("127."):
        return [primary]   # the address your router gave this machine
    try:
        found = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        found = set()
    return sorted(ip for ip in found if ipaddress.ip_address(ip).is_private and not ip.startswith("127."))


def cmd_events(args, cfg) -> int:
    from datetime import date, timedelta

    from swing_agent import db
    from swing_agent.events import add_event, delete_event, get_events, refresh_earnings, sync_events_csv

    conn = db.connect()
    if args.action == "add":
        d = _parse_date(args.date)
        new = add_event(conn, d, args.symbol, args.type, args.note or "")
        print(f"Added event #{new}." if new else "That event is already in the calendar.")
        print(f"Tip: to keep it permanently in a file you can edit, add it to {cfg.data.events_file} instead.")
    elif args.action == "delete":
        print(f"Deleted event #{args.id}." if delete_event(conn, args.id) else f"No event #{args.id}.")
    elif args.action == "sync":
        added, problems = sync_events_csv(conn, cfg.resolve(cfg.data.events_file))
        print(f"Synced {added} event(s) from {cfg.data.events_file}.")
        for p in problems:
            print("  " + p)
    elif args.action == "fetch":
        syms = db.get_symbols(conn)["symbol"].tolist()
        print(f"Fetching results dates from Yahoo for {len(syms)} symbols (about a minute)...")
        print(refresh_earnings(conn, syms, force=True))
    else:  # list
        start = date.today() - timedelta(days=args.past)
        ev = get_events(conn, start=start, end=date.today() + timedelta(days=args.days))
        print(ev.to_string(index=False) if not ev.empty else "No events in that window.")
    return 0


def cmd_schedule(args, cfg) -> int:
    import platform
    import subprocess

    from swing_agent.scheduling import instructions, windows_powershell

    print(instructions(cfg.schedule.run_time_ist))
    if args.install:
        if platform.system() != "Windows":
            print("\n--install only works on Windows. Add the cron line above with `crontab -e`.")
            return 1
        print("\nInstalling the scheduled task...")
        return subprocess.call(["powershell", "-NoProfile", "-Command",
                                windows_powershell(cfg.schedule.run_time_ist)])
    return 0


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
    s.add_argument("--log-file", help="Append the report and any error to this file instead of printing")
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

    s = sub.add_parser("capital", help="Show or set your trading capital (used for all position sizes)")
    s.add_argument("amount", nargs="?", type=float, help="New starting capital in ₹, e.g. 250000")
    s.set_defaults(func=cmd_capital)

    s = sub.add_parser("analyze", help="Analyse any NSE stock: rule checklist, plan, target date")
    s.add_argument("symbol", help="Trading symbol, e.g. TCS, RELIANCE, M&M (same symbol for BSE)")
    s.add_argument("--exchange", type=str.upper, choices=["NSE", "BSE"], default="NSE",
                   help="Stock exchange (default NSE). TCS.BO / TCS.NS also work.")
    s.add_argument("--no-download", action="store_true", help="Use stored prices only (offline)")
    s.add_argument("--add", action="store_true", help="Also add it to the watchlist for the daily scan")
    s.add_argument("--capital", type=float, metavar="RUPEES",
                   help="What-if: size the plan as if your capital were this amount (saved capital unchanged)")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("dashboard", help="Launch the Streamlit dashboard")
    s.add_argument("--port", type=int, default=8501)
    s.add_argument("--headless", action="store_true", help="Don't open a browser window")
    s.add_argument("--lan", action="store_true",
                   help="Also allow phones/PCs on your local network to connect (no login!)")
    s.set_defaults(func=cmd_dashboard)

    e = sub.add_parser("events", help="Event calendar (results dates, RBI, Budget, Fed...)")
    esub = e.add_subparsers(dest="action", required=True)
    s = esub.add_parser("add", help="Add one event")
    s.add_argument("--date", required=True, help="YYYY-MM-DD")
    s.add_argument("--symbol", required=True, help="NSE symbol, or ALL for market-wide events")
    s.add_argument("--type", required=True, type=str.upper,
                   choices=["RESULTS", "RBI", "BUDGET", "FED", "ELECTION", "OTHER"])
    s.add_argument("--note")
    s = esub.add_parser("list", help="Show events (default: last 7 and next 60 days)")
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--past", type=int, default=7)
    s = esub.add_parser("delete", help="Delete an event by id")
    s.add_argument("id", type=int)
    esub.add_parser("sync", help="Load data/events.csv into the database")
    esub.add_parser("fetch", help="Fetch results dates from Yahoo now (best effort)")
    e.set_defaults(func=cmd_events)

    s = sub.add_parser("schedule", help="Show how to run `run` automatically on weekdays")
    s.add_argument("--install", action="store_true", help="Windows only: create the scheduled task")
    s.set_defaults(func=cmd_schedule)

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
    except (ConfigError, WatchlistError, DatabaseError, NoDataError, TradeError, PortfolioError, EventError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
