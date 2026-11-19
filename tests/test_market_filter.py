import pandas as pd
import pytest

from fixtures.patterns import index_series
from swing_agent.market_filter import (
    BEARISH,
    BULLISH,
    CAUTION,
    FEAR,
    compute_regime_series,
    regime_at,
    regime_change_message,
    regime_params,
)


def _vix(index_df, values):
    return pd.DataFrame({"close": values}, index=index_df.index)


@pytest.mark.parametrize("kind,expected", [("bull", BULLISH), ("caution", CAUTION), ("bear", BEARISH)])
def test_regimes_from_synthetic_nifty(cfg, kind, expected):
    idx = index_series(kind)
    series = compute_regime_series(idx, _vix(idx, [15.0] * len(idx)), cfg)
    assert series["regime"].iloc[-1] == expected


def test_high_vix_gives_fear(cfg):
    idx = index_series("bull")
    series = compute_regime_series(idx, _vix(idx, [cfg.market_filter.vix_max + 1] * len(idx)), cfg)
    assert series["regime"].iloc[-1] == FEAR


def test_vix_spike_gives_fear(cfg):
    idx = index_series("bull")
    vals = [12.0] * len(idx)
    vals[-1] = 12.0 * (1 + (cfg.market_filter.vix_spike_pct_5d + 5) / 100)  # below vix_max but +35% in 5 days
    assert vals[-1] < cfg.market_filter.vix_max
    series = compute_regime_series(idx, _vix(idx, vals), cfg)
    assert series["regime"].iloc[-1] == FEAR
    assert series["regime"].iloc[-2] == BULLISH


def test_missing_vix_does_not_crash(cfg):
    series = compute_regime_series(index_series("bull"), None, cfg)
    assert series["regime"].iloc[-1] == BULLISH


def test_regime_state_and_params(cfg):
    idx = index_series("caution")
    state = regime_at(compute_regime_series(idx, None, cfg), idx.index[-1], cfg)
    assert state.regime == CAUTION and state.allows_entries
    assert state.risk_multiplier == cfg.market_filter.caution_risk_multiplier
    assert state.max_positions == cfg.market_filter.caution_max_positions
    assert regime_params(BEARISH, cfg)[0] is False
    assert regime_params(FEAR, cfg)[0] is False
    assert regime_params(BULLISH, cfg) == (True, 1.0, cfg.account.max_open_positions)


def test_regime_change_message():
    assert regime_change_message(None, BULLISH) is None
    assert regime_change_message(BULLISH, BULLISH) is None
    assert "BULLISH to FEAR" in regime_change_message(BULLISH, FEAR)
