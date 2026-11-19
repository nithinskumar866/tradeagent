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

    for name, ms, helptext in [
        ("scan", 2, "Regime + signals + exits, print report"),
        ("run", 3, "update + scan + save report"),
        ("trade", 3, "Log a buy or sell"),
        ("backtest", 4, "Run the backtester"),
        ("dashboard", 5, "Launch the Streamlit dashboard"),
        ("events", 6, "Manage the event calendar"),
    ]:
        s = sub.add_parser(name, help=f"{helptext} (milestone {ms})")
        s.set_defaults(func=_not_yet(ms))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(console_level=logging.INFO if args.verbose else logging.ERROR)
    try:
        cfg = load_config(args.config)
        return args.func(args, cfg)
    except (ConfigError, WatchlistError, DatabaseError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
