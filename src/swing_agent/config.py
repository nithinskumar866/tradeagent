"""Load and validate ``config.yaml``.

Every strategy number comes from here. Validation uses pydantic v2 and fails with a
clear, human-readable message (``ConfigError``) on bad values.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"


class ConfigError(Exception):
    """Raised when config.yaml is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCfg(_Section):
    capital: float = Field(gt=0)
    risk_per_trade_pct: float = Field(gt=0, le=10)
    max_open_positions: int = Field(ge=1)
    max_position_pct: float = Field(gt=0, le=100)
    max_per_sector: int = Field(ge=1)


class UniverseCfg(_Section):
    watchlist_file: str
    min_price: float = Field(ge=0)
    min_avg_traded_value_cr: float = Field(ge=0)
    min_history_bars: int = Field(ge=1)


class DataCfg(_Section):
    source: Literal["yfinance"] = "yfinance"
    history_years: int = Field(ge=1, le=30)
    index_symbol: str
    vix_symbol: str
    refetch_overlap_days: int = Field(ge=1, le=60)
    events_file: str = "data/events.csv"
    fetch_results_dates: bool = True


class MarketFilterCfg(_Section):
    ema_fast: int = Field(ge=2)
    ema_slow: int = Field(ge=2)
    vix_max: float = Field(gt=0)
    vix_spike_pct_5d: float = Field(gt=0)
    caution_risk_multiplier: float = Field(gt=0, le=1)
    caution_max_positions: int = Field(ge=0)

    @model_validator(mode="after")
    def _fast_lt_slow(self) -> "MarketFilterCfg":
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be smaller than ema_slow")
        return self


class EntryCfg(_Section):
    ema_short: int = Field(ge=2)
    ema_mid: int = Field(ge=2)
    ema_long: int = Field(ge=2)
    ema_long_slope_lookback: int = Field(ge=1)
    pullback_min_pct: float = Field(ge=0)
    pullback_max_pct: float = Field(gt=0, le=100)
    pullback_lookback: int = Field(ge=2)
    touch_tolerance_pct: float = Field(ge=0)
    touch_lookback: int = Field(ge=1)
    rsi_period: int = Field(ge=2)
    rsi_dip_min: float = Field(ge=0, le=100)
    rsi_dip_max: float = Field(ge=0, le=100)
    rsi_dip_lookback: int = Field(ge=1)
    buy_zone_top_buffer_pct: float = Field(ge=0)
    avoid_results_within_days: int = Field(ge=0)
    pullback_low_lookback: int = Field(default=5, ge=1)
    rs_lookback: int = Field(default=63, ge=1)
    volume_avg_days: int = Field(default=20, ge=2)
    volume_dryup_days: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def _ranges(self) -> "EntryCfg":
        if not self.ema_short < self.ema_mid < self.ema_long:
            raise ValueError("EMAs must satisfy ema_short < ema_mid < ema_long")
        if self.pullback_min_pct >= self.pullback_max_pct:
            raise ValueError("pullback_min_pct must be smaller than pullback_max_pct")
        if self.rsi_dip_min >= self.rsi_dip_max:
            raise ValueError("rsi_dip_min must be smaller than rsi_dip_max")
        return self


class StopsTargetsCfg(_Section):
    swing_low_lookback: int = Field(ge=1)
    stop_buffer_pct: float = Field(ge=0, lt=100)
    atr_period: int = Field(ge=2)
    min_stop_atr: float = Field(ge=0)
    max_stop_pct: float = Field(gt=0, lt=100)
    target_r: float = Field(gt=0)
    breakeven_at_r: float = Field(gt=0)
    trend_exit_ema: int = Field(ge=2)
    time_stop_days: int = Field(ge=1)
    results_warning_days: int = Field(ge=0)


class CostsCfg(_Section):
    brokerage_per_order: float = Field(ge=0)
    stt_pct: float = Field(ge=0)
    exchange_txn_pct: float = Field(ge=0)
    sebi_fee_pct: float = Field(ge=0)
    stamp_duty_buy_pct: float = Field(ge=0)
    gst_pct: float = Field(ge=0)
    dp_charge_per_sell: float = Field(ge=0)
    slippage_pct: float = Field(ge=0)


class BacktestCfg(_Section):
    start_date: date
    in_sample_end: date
    initial_capital: float = Field(gt=0)

    @model_validator(mode="after")
    def _dates(self) -> "BacktestCfg":
        if self.in_sample_end <= self.start_date:
            raise ValueError("in_sample_end must be after start_date")
        return self


class ScheduleCfg(_Section):
    run_time_ist: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class ExplainerCfg(_Section):
    enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    model: str = "llama3.1:8b"


class Config(_Section):
    """The full, validated configuration."""

    account: AccountCfg
    universe: UniverseCfg
    data: DataCfg
    market_filter: MarketFilterCfg
    entry: EntryCfg
    stops_targets: StopsTargetsCfg
    costs: CostsCfg
    backtest: BacktestCfg
    schedule: ScheduleCfg
    explainer: ExplainerCfg = ExplainerCfg()

    def resolve(self, relative: str | Path) -> Path:
        """Resolve a path from config relative to the project root."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def watchlist_path(self) -> Path:
        """Absolute path of the watchlist CSV."""
        return self.resolve(self.universe.watchlist_file)


def _format_validation_error(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "(root)"
        lines.append(f"  - {loc}: {e['msg']}")
    return "Invalid config.yaml:\n" + "\n".join(lines)


def parse_config(raw: dict) -> Config:
    """Validate a raw dict (as loaded from YAML) into a ``Config``."""
    try:
        return Config.model_validate(raw)
    except ValidationError as err:
        raise ConfigError(_format_validation_error(err)) from None


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the YAML config file. Raises ``ConfigError`` with a friendly message."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        raise ConfigError(f"config.yaml is not valid YAML: {err}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a mapping of sections (account, universe, ...).")
    return parse_config(raw)


def set_capital(amount: float, path: str | Path | None = None) -> Config:
    """Change ``account.capital`` in config.yaml, keeping the file's comments and layout.

    Validates the new value (and the whole file) before saving; raises ConfigError if invalid.
    """
    import re

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    text = cfg_path.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^account:\s*\n(?:[ \t]+.*\n)*?([ \t]+capital:[ \t]*)([0-9_.]+)", text)
    if not m:
        raise ConfigError("Couldn't find account.capital in config.yaml; edit it by hand.")
    value = int(amount) if float(amount).is_integer() else float(amount)
    new_text = text[: m.start(2)] + str(value) + text[m.end(2):]
    parsed = parse_config(yaml.safe_load(new_text))  # raises ConfigError on a bad value
    cfg_path.write_text(new_text, encoding="utf-8")
    return parsed
