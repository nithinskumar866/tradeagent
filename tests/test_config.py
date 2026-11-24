import copy

import pytest
import yaml

from conftest import ROOT
from swing_agent.config import ConfigError, load_config, parse_config


def _raw():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_default_config_is_valid(cfg):
    assert cfg.account.capital > 0
    assert cfg.data.index_symbol == "^NSEI"
    assert cfg.watchlist_path.name == "nifty100.csv"


@pytest.mark.parametrize(
    "section,key,value,needle",
    [
        ("account", "capital", -5, "account.capital"),
        ("account", "max_open_positions", 0, "account.max_open_positions"),
        ("entry", "pullback_min_pct", 20, "pullback_min_pct must be smaller"),
        ("entry", "rsi_dip_min", 60, "rsi_dip_min must be smaller"),
        ("entry", "ema_short", 300, "ema_short < ema_mid < ema_long"),
        ("schedule", "run_time_ist", "25:00", "schedule.run_time_ist"),
        ("backtest", "in_sample_end", "2010-01-01", "in_sample_end must be after"),
    ],
)
def test_bad_values_give_clear_errors(section, key, value, needle):
    raw = copy.deepcopy(_raw())
    raw[section][key] = value
    with pytest.raises(ConfigError) as exc:
        parse_config(raw)
    assert needle in str(exc.value)


def test_unknown_key_rejected():
    raw = copy.deepcopy(_raw())
    raw["account"]["capitol"] = 5
    with pytest.raises(ConfigError, match="capitol"):
        parse_config(raw)


def test_missing_section_rejected():
    raw = copy.deepcopy(_raw())
    del raw["costs"]
    with pytest.raises(ConfigError, match="costs"):
        parse_config(raw)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("account: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_set_capital_keeps_comments_and_validates(tmp_path):
    import shutil

    from swing_agent.config import set_capital

    p = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", p)
    cfg = set_capital(250000, p)
    assert cfg.account.capital == 250000 and cfg.backtest.initial_capital == 100000
    text = p.read_text(encoding="utf-8")
    assert "  capital: 250000            # INR" in text
    with pytest.raises(ConfigError, match="account.capital"):
        set_capital(0, p)
    assert load_config(p).account.capital == 250000   # bad value was not saved
