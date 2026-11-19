"""Watchlist loading/validation and refresh from niftyindices.com."""

from __future__ import annotations

import io
import logging
import shutil
import urllib.request
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

NIFTY100_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
REQUIRED_COLUMNS = ["symbol", "name", "sector"]

MANUAL_UPDATE_HELP = (
    "To update it manually: open https://www.niftyindices.com → Indices → NIFTY 100 → "
    "download the constituents CSV, then save it as data/watchlists/nifty100.csv with "
    "columns symbol,name,sector (Symbol → symbol, Company Name → name, Industry → sector)."
)


class WatchlistError(Exception):
    """Raised for a missing, malformed or empty watchlist."""


def clean_watchlist(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise columns/values, drop blanks and duplicates. Raises if empty or columns missing."""
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise WatchlistError(f"Watchlist is missing columns: {missing}. Expected symbol,name,sector.")
    df = df[REQUIRED_COLUMNS].copy()
    for c in REQUIRED_COLUMNS:
        df[c] = df[c].fillna("").astype(str).str.strip()
    df["symbol"] = df["symbol"].str.upper()
    df.loc[df["sector"] == "", "sector"] = "Unknown"
    df = df[df["symbol"] != ""].drop_duplicates(subset="symbol").reset_index(drop=True)
    if df.empty:
        raise WatchlistError("The watchlist is empty. Add at least one symbol (columns symbol,name,sector).")
    return df


def load_watchlist(path: str | Path) -> pd.DataFrame:
    """Read and validate a watchlist CSV with columns symbol,name,sector."""
    path = Path(path)
    if not path.exists():
        raise WatchlistError(f"Watchlist file not found: {path}")
    return clean_watchlist(pd.read_csv(path, dtype=str))


def parse_niftyindices_csv(content: bytes) -> pd.DataFrame:
    """Convert a niftyindices.com constituents CSV into symbol,name,sector."""
    raw = pd.read_csv(io.BytesIO(content), dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    try:
        df = pd.DataFrame(
            {"symbol": raw["Symbol"], "name": raw["Company Name"], "sector": raw["Industry"]}
        )
    except KeyError as err:
        raise WatchlistError(f"Unexpected niftyindices CSV format (missing {err}).") from None
    return clean_watchlist(df)


def refresh_watchlist(path: str | Path, url: str = NIFTY100_URL, timeout: int = 20) -> pd.DataFrame:
    """Download the latest constituents and overwrite ``path`` (old file kept as .bak).

    On any failure the existing file is left untouched and ``WatchlistError`` explains
    how to update it manually.
    """
    path = Path(path)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (personal swing-agent)", "Accept": "text/csv,*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
        df = parse_niftyindices_csv(content)
    except WatchlistError as err:
        raise WatchlistError(f"{err} Kept the existing file. {MANUAL_UPDATE_HELP}") from None
    except Exception as err:
        raise WatchlistError(
            f"Could not download the constituents list ({err}). Kept the existing file. {MANUAL_UPDATE_HELP}"
        ) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    df.to_csv(path, index=False)
    log.info("Watchlist refreshed: %d symbols written to %s", len(df), path)
    return df
