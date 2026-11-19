import numpy as np
import pandas as pd
import pytest

from fixtures.synthetic import make_ohlcv
from swing_agent.indicators import (
    atr_wilder,
    avg_traded_value_cr,
    ema,
    relative_strength,
    rsi_wilder,
    sma,
)


def test_ema_matches_pandas_ewm():
    s = make_ohlcv(100, seed=7).set_index("date")["close"]
    pd.testing.assert_series_equal(ema(s, 20), s.ewm(span=20, adjust=False).mean())


def test_sma():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert sma(s, 3).isna().sum() == 2


def test_rsi_hand_computed():
    # closes 10, 11, 10.5, 11.5, 11 with n=2 (alpha = 0.5)
    # gains 1, 0, 1, 0 / losses 0, .5, 0, .5
    # avg gain 1, .5, .75, .375 / avg loss 0, .25, .125, .3125 (first value seeds)
    # RSI from the 2nd observation: 100-100/(1+2), 100-100/(1+6), 100-100/(1+1.2)
    rsi = rsi_wilder(pd.Series([10, 11, 10.5, 11.5, 11]), 2)
    assert rsi.iloc[:2].isna().all()
    assert rsi.iloc[2:].tolist() == pytest.approx([66.6667, 85.7143, 54.5455], abs=1e-3)


def test_rsi_bounds_and_edge_cases():
    s = make_ohlcv(500, seed=3, vol=0.03).set_index("date")["close"]
    r = rsi_wilder(s, 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert rsi_wilder(pd.Series(np.arange(1.0, 40)), 14).dropna().eq(100).all()       # only gains
    assert rsi_wilder(pd.Series(np.arange(40.0, 1, -1)), 14).dropna().eq(0).all()     # only losses


def test_atr_hand_computed():
    df = pd.DataFrame({"high": [10, 11, 10], "low": [8, 9, 7], "close": [9, 10.5, 8]}, dtype=float)
    # TR: 2, max(2, |11-9|, |9-9|)=2, max(3, |10-10.5|, |7-10.5|)=3.5 → ATR(2): -, 2, 2.75
    atr = atr_wilder(df, 2)
    assert np.isnan(atr.iloc[0])
    assert atr.iloc[1:].tolist() == pytest.approx([2.0, 2.75])


def test_avg_traded_value_cr():
    df = pd.DataFrame({"close": [100.0] * 20, "volume": [1_000_000] * 20})
    assert avg_traded_value_cr(df, 20).iloc[-1] == pytest.approx(10.0)  # 1e8 / 1e7


def test_relative_strength_aligns_index():
    idx_dates = pd.bdate_range("2024-01-01", periods=70)
    stock = pd.Series(np.linspace(100, 120, 70), index=idx_dates)
    index = pd.Series(np.linspace(1000, 1050, 70), index=idx_dates)
    rs = relative_strength(stock, index, 63)
    exp = (stock.iloc[-1] / stock.iloc[-64] - 1 - (index.iloc[-1] / index.iloc[-64] - 1)) * 100
    assert rs.iloc[-1] == pytest.approx(exp)
    # stock with a missing day still aligns to past index values only
    rs2 = relative_strength(stock.drop(idx_dates[30]), index, 63)
    assert rs2.notna().sum() > 0
