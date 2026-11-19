"""Incremental price updates and data-quality checks.

* First run: download ``history_years`` for the watchlist + index + VIX.
* Later runs: fetch from a few bars before the last stored bar and upsert.
* Corporate-action check: if an overlapping stored close differs from the freshly
  downloaded close by more than 1 %, the history was re-adjusted, so the symbol's
  full history is downloaded again.
* If every download fails (e.g. no internet), cached data is kept and the report is
  flagged ``offline`` so callers can warn that data may be stale.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import db
from .config import Config
from .data_sources import DataSource, DataSourceError

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
READJUST_THRESHOLD = 0.01        # 1 % close mismatch on overlapping bars
MAX_GAP_CALENDAR_DAYS = 7
MARKET_DATA_FINAL_IST_HOUR = 16  # bars for "today" are only trusted after 16:00 IST


@dataclass
class QualityIssue:
    """One data-quality finding, aggregated per symbol and kind."""

    symbol: str
    kind: str  # STALE | ZERO_VOLUME | BAD_PRICE | HIGH_LT_LOW | GAP | FAILED
    message: str
    count: int = 1
    latest_date: str | None = None


@dataclass
class UpdateReport:
    """Outcome of a data update."""

    new_bars: dict[str, int] = field(default_factory=dict)
    full_downloads: list[str] = field(default_factory=list)
    readjusted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    issues: list[QualityIssue] = field(default_factory=list)
    latest_trading_day: date | None = None
    offline: bool = False

    @property
    def total_new_bars(self) -> int:
        """Sum of new bars across symbols."""
        return sum(self.new_bars.values())


def now_ist() -> datetime:
    """Current time in India (IST has no DST, so a fixed offset is exact)."""
    return datetime.now(IST)


def latest_trading_day(conn: sqlite3.Connection, index_symbol: str) -> date | None:
    """Latest trading day = date of the latest stored Nifty bar (no holiday calendar needed)."""
    return db.last_price_date(conn, index_symbol)


def drop_phantom_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Drop placeholder bars: zero volume with open == high == low == close.

    Yahoo sometimes fills holidays or missing sessions for NSE stocks with a flat,
    zero-volume bar. These are not real trading and would distort ATR, swing lows and
    volume averages. Only apply to stocks (indices legitimately report zero volume).
    """
    if df.empty:
        return df
    flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"])
    phantom = flat & (df["volume"].fillna(0) <= 0)
    if phantom.any():
        log.info("Dropping %d placeholder bar(s): %s", int(phantom.sum()),
                 ", ".join(d.strftime("%Y-%m-%d") for d in df.loc[phantom, "date"]))
    return df[~phantom].reset_index(drop=True)


class DataFetcher:
    """Keeps ``prices`` up to date from a ``DataSource``."""

    def __init__(self, conn: sqlite3.Connection, source: DataSource, cfg: Config) -> None:
        self.conn = conn
        self.source = source
        self.cfg = cfg
        self._volumeless = {cfg.data.index_symbol, cfg.data.vix_symbol}

    # ------------------------------------------------------------ helpers
    def universe(self) -> list[str]:
        """Index + VIX first, then the active watchlist symbols."""
        syms = db.get_symbols(self.conn)["symbol"].tolist()
        return [self.cfg.data.index_symbol, self.cfg.data.vix_symbol] + syms

    def _history_start(self, today: date) -> date:
        return today - timedelta(days=int(365.25 * self.cfg.data.history_years))

    @staticmethod
    def _drop_incomplete_today(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
        """Drop a bar dated today if the market data for today isn't final yet."""
        if df.empty or now.hour >= MARKET_DATA_FINAL_IST_HOUR:
            return df
        today = pd.Timestamp(now.date())
        return df[df["date"] < today]

    # ------------------------------------------------------------ update
    def update(
        self,
        full: bool = False,
        symbols: list[str] | None = None,
        now: datetime | None = None,
        quality_since: date | None = None,
    ) -> UpdateReport:
        """Download new bars for ``symbols`` (default: whole universe) and run quality checks.

        Args:
            full: re-download the full history for every symbol.
            symbols: restrict to these symbols.
            now: current IST time (injectable for tests).
            quality_since: only report quality issues on bars from this date on
                (default: last 60 days for incremental runs, all history for full ones).
        """
        now = now or now_ist()
        today = now.date()
        report = UpdateReport()
        symbols = symbols or self.universe()

        # Plan: group symbols by fetch start date so they can be batch-downloaded.
        plan: dict[date, list[str]] = defaultdict(list)
        last_dates: dict[str, date | None] = {}
        for sym in symbols:
            last = None if full else db.last_price_date(self.conn, sym)
            last_dates[sym] = last
            if last is None:
                plan[self._history_start(today)].append(sym)
            else:
                start = db.nth_last_price_date(self.conn, sym, self.cfg.data.refetch_overlap_days)
                plan[start or last].append(sym)

        for start, group in sorted(plan.items()):
            log.info("Fetching %d symbols from %s to %s", len(group), start, today)
            try:
                fetched = self.source.fetch_daily_batch(group, start, today)
            except DataSourceError as err:
                log.warning("Batch download failed: %s", err)
                for sym in group:
                    report.failed[sym] = str(err)
                continue
            for sym in group:
                df = fetched.get(sym)
                if df is None or df.empty:
                    report.failed[sym] = "no data returned"
                    log.warning("%s: no data returned", sym)
                    continue
                df = self._drop_incomplete_today(df, now)
                if sym not in self._volumeless:
                    df = drop_phantom_bars(df)
                try:
                    self._store(sym, df, last_dates[sym], now, report)
                except DataSourceError as err:
                    report.failed[sym] = str(err)

        report.offline = bool(symbols) and len(report.failed) == len(symbols)
        if report.offline:
            log.warning("All downloads failed; using cached data, which may be stale.")

        report.latest_trading_day = latest_trading_day(self.conn, self.cfg.data.index_symbol)
        if quality_since is None and not full and any(v is not None for v in last_dates.values()):
            quality_since = today - timedelta(days=60)
        report.issues = check_quality(
            self.conn,
            symbols=[s for s in symbols if s not in report.failed],
            index_symbol=self.cfg.data.index_symbol,
            no_volume_symbols={self.cfg.data.index_symbol, self.cfg.data.vix_symbol},
            since=quality_since,
        )
        report.issues += [
            QualityIssue(sym, "FAILED", f"{sym}: download failed ({why}); skipped today")
            for sym, why in report.failed.items()
        ]
        return report

    def _store(
        self, sym: str, df: pd.DataFrame, last: date | None, now: datetime, report: UpdateReport
    ) -> None:
        today = now.date()
        if last is None:
            db.replace_prices(self.conn, sym, df)
            report.full_downloads.append(sym)
            report.new_bars[sym] = len(df)
            return

        stored = db.get_prices(self.conn, sym, start=df["date"].min().date())
        if self._was_readjusted(stored, df):
            log.info("%s: overlapping closes changed by >1%%; re-downloading full history", sym)
            full_df = self.source.fetch_daily(sym, self._history_start(today), today)
            full_df = drop_phantom_bars(self._drop_incomplete_today(full_df, now))
            db.replace_prices(self.conn, sym, full_df)
            report.readjusted.append(sym)
            report.new_bars[sym] = int((full_df["date"] > pd.Timestamp(last)).sum())
            return

        db.upsert_prices(self.conn, sym, df)
        report.new_bars[sym] = int((df["date"] > pd.Timestamp(last)).sum())

    @staticmethod
    def _was_readjusted(stored: pd.DataFrame, new: pd.DataFrame) -> bool:
        """True if any overlapping close differs by more than READJUST_THRESHOLD."""
        if stored.empty:
            return False
        new_close = new.set_index("date")["close"]
        common = stored.index.intersection(new_close.index)
        if common.empty:
            return False
        old = stored.loc[common, "close"]
        diff = (new_close.loc[common] - old).abs() / old.abs()
        return bool((diff > READJUST_THRESHOLD).any())


# ---------------------------------------------------------------- quality

def check_quality(
    conn: sqlite3.Connection,
    symbols: list[str],
    index_symbol: str,
    no_volume_symbols: set[str] | None = None,
    since: date | None = None,
) -> list[QualityIssue]:
    """Run data-quality checks on stored prices and return aggregated issues.

    Checks: stale latest bar (older than the latest index bar), zero/missing volume,
    non-positive or missing prices, high < low, and gaps over 7 calendar days.
    """
    no_volume_symbols = no_volume_symbols or set()
    latest = latest_trading_day(conn, index_symbol)
    issues: list[QualityIssue] = []
    since_ts = pd.Timestamp(since) if since else None

    for sym in symbols:
        df = db.get_prices(conn, sym)
        if df.empty:
            issues.append(QualityIssue(sym, "STALE", f"{sym}: no price data stored"))
            continue
        last = df.index[-1].date()
        if latest and sym != index_symbol and last < latest:
            issues.append(
                QualityIssue(sym, "STALE", f"{sym}: latest bar {last} is older than Nifty's {latest}",
                             latest_date=str(last))
            )

        gaps = df.index.to_series().diff().dt.days
        window = df if since_ts is None else df[df.index >= since_ts]
        gaps = gaps if since_ts is None else gaps[gaps.index >= since_ts]
        if window.empty:
            continue

        checks: list[tuple[str, pd.Series, str]] = [
            ("BAD_PRICE",
             window[["open", "high", "low", "close"]].isna().any(axis=1)
             | (window[["open", "high", "low", "close"]] <= 0).any(axis=1),
             "missing or non-positive prices"),
            ("HIGH_LT_LOW", window["high"] < window["low"], "high below low"),
            ("GAP", gaps > MAX_GAP_CALENDAR_DAYS, f"gaps over {MAX_GAP_CALENDAR_DAYS} calendar days"),
        ]
        if sym not in no_volume_symbols:
            checks.append(
                ("ZERO_VOLUME", window["volume"].isna() | (window["volume"] <= 0), "zero or missing volume")
            )
        for kind, mask, label in checks:
            n = int(mask.sum())
            if n:
                last_bad = str(mask[mask].index[-1].date())
                issues.append(
                    QualityIssue(sym, kind, f"{sym}: {n} bar(s) with {label} (latest {last_bad})",
                                 count=n, latest_date=last_bad)
                )

    for i in issues:
        log.warning("Data quality: %s", i.message)
    return issues
