"""Tests for BrakedHoldV2.py (200DMA braked-hold + vol-targeted sizing + 2% exit
band), backtest_india_tax.py (the India after-tax CLI wrapper), and inr_data.py
(the shared Indian-exchange data adapters).

Offline only: reads the cached BTC CSV / cached INR JSON in nifty_backtest/cache,
no live network, no exchange, no Telegram.
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "nifty_backtest"))
sys.path.insert(0, os.path.join(HERE, "..", "user_data", "strategies"))

import btc_faber_backtest as bt
import inr_data
import backtest_india_tax as bit
from BrakedHoldV2 import (
    BrakedHoldV2,
    realized_vol,
    vol_target_weight,
    should_rebalance,
    TARGET_VOL,
    REBAL_BAND,
    MAX_WEIGHT,
)


def _strategy():
    cfg = {"stake_currency": "USDT", "stake_amount": 100, "dry_run": True,
           "timeframe": "1d", "strategy": "BrakedHoldV2"}
    return BrakedHoldV2(cfg)


def _btc_dataframe():
    df = bt.load_csv("BTC_USD_3y.csv")
    # freqtrade strategy dataframes use lowercase OHLCV column names
    return df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                               "Low": "low", "Close": "close", "Volume": "volume"})


# ---- 1. strategy entry/exit signals ----

def test_entry_signal_rising_edge_of_200dma():
    s = _strategy()
    df = _btc_dataframe()
    df = s.populate_indicators(df, {"pair": "BTC/USDT"})
    df = s.populate_entry_trend(df, {"pair": "BTC/USDT"})
    assert "enter_long" in df.columns
    entries = df[df["enter_long"] == 1]
    assert len(entries) > 0
    assert (entries["close"] > entries["sma200"]).all()


def test_exit_signal_uses_2pct_band_not_raw_cross():
    s = _strategy()
    df = _btc_dataframe()
    df = s.populate_indicators(df, {"pair": "BTC/USDT"})
    df = s.populate_exit_trend(df, {"pair": "BTC/USDT"})
    assert "exit_long" in df.columns
    exits = df[df["exit_long"] == 1]
    assert len(exits) > 0
    # every exit must be below the BANDED brake (2% under sma200), not just under sma200
    assert (exits["close"] < exits["sma200"] * s.EXIT_BAND).all()
    # the chop zone (below sma200 but still inside the 2% band) must NOT exit
    chop = df[(df["close"] < df["sma200"]) & (df["close"] >= df["sma200"] * s.EXIT_BAND)]
    if len(chop):
        assert (chop["exit_long"] != 1).all()


def test_no_hard_stop_and_no_roi():
    s = _strategy()
    assert s.stoploss == -0.99
    assert s.minimal_roi == {}
    assert s.use_exit_signal is True


# ---- 2. India after-tax formula ----

def test_after_tax_losing_trade_no_offset():
    """Reuses btc_faber_backtest's engine (the one true after-tax formula) --
    a losing trade must show net_ret == -TDS, never a tax credit."""
    df = bt.load_csv("BTC_USD_3y.csv")
    d = df.iloc[:3].copy()
    d["Close"] = [100.0, 100.0, 90.0]
    d["Low"] = [100.0, 100.0, 89.0]
    d["High"] = [100.0, 100.0, 100.0]
    e = pd.Series([True, False, False])
    x = pd.Series([False, False, False])
    _, trades = bt.backtest_tax(d, e, x, stop=-1.0)
    assert trades[0]["gross_ret"] < 0
    assert trades[0]["net_ret"] == pytest.approx(-bt.TDS, abs=1e-9)


def test_dca_benchmark_losing_scenario_no_offset():
    """backtest_india_tax's DCA-into-BTC benchmark applies the SAME
    no-loss-offset formula on its single final sale."""
    dates = pd.date_range("2024-01-01", periods=40, freq="D")
    prices = np.linspace(100, 50, 40)   # steady decline -> guaranteed loss
    df = pd.DataFrame({"Date": dates, "Close": prices})
    eq, trades = bit.dca_benchmark(df, interval_days=10)
    assert len(trades) == 1
    assert trades[0]["gross_ret"] < 0
    assert trades[0]["net_ret"] == pytest.approx(-bt.TDS, abs=1e-9)
    assert eq.iloc[-1] == pytest.approx(1.0 - bt.TDS, abs=1e-9)


def test_backtest_india_tax_cli_runs_clean():
    """The CLI wrapper's run() should complete end-to-end on cached data (no
    network) and produce a metrics dict for both the strategy and the benchmark."""
    result = bit.run("BrakedHoldV2", "BTC/USDT", 365)
    for key in ("total_return_pct", "cagr_pct", "max_drawdown_pct", "trades",
                "tax_drag_pct"):
        assert key in result["strategy"]
        assert key in result["dca_benchmark"]


# ---- 3. INR data adapters ----

def test_inr_adapters_return_valid_ohlcv():
    fetchers = [
        lambda: inr_data.fetch_wazirx_inr(limit=365),
        lambda: inr_data.fetch_coindcx_inr(limit=365),
        lambda: inr_data.fetch_coingecko_inr(days=365),
    ]
    for fetch in fetchers:
        df = fetch()
        assert len(df) > 0
        assert all(c in df.columns for c in ["Date", "Open", "High", "Low", "Close", "Volume"])
        # timestamps should be real dates, not the 1970 epoch
        assert df["Date"].iloc[0].year >= 2020
        assert (df["Close"] > 0).all()
        assert df["Date"].is_monotonic_increasing


def test_btc_faber_backtest_reexports_inr_data_functions():
    """btc_faber_backtest.py imports from inr_data.py rather than duplicating it --
    the re-exported names must resolve to the SAME function objects."""
    assert bt.fetch_wazirx_inr is inr_data.fetch_wazirx_inr
    assert bt.fetch_coindcx_inr is inr_data.fetch_coindcx_inr
    assert bt.fetch_coingecko_inr is inr_data.fetch_coingecko_inr


# ---- 4. vol-targeting sizing ----

def test_realized_vol_needs_full_window():
    assert np.isnan(realized_vol([0.01] * 5, window=20))
    vol = realized_vol([0.01] * 25, window=20)
    assert vol > 0


def test_vol_target_weight_scales_down_when_vol_hot():
    calm = vol_target_weight(vol=0.10, target_vol=TARGET_VOL)   # below target -> full size
    hot = vol_target_weight(vol=1.00, target_vol=TARGET_VOL)    # way above target -> scaled down
    assert calm == MAX_WEIGHT
    assert 0 < hot < MAX_WEIGHT
    assert hot == pytest.approx(TARGET_VOL / 1.00)


def test_vol_target_weight_nan_falls_back_to_full_size():
    assert vol_target_weight(float("nan")) == MAX_WEIGHT


def test_should_rebalance_respects_band():
    assert should_rebalance(0.5, None) is True                       # never sized -- always size
    assert should_rebalance(0.55, 0.5, band=REBAL_BAND) is False      # inside 25% band -- hold
    assert should_rebalance(0.7, 0.5, band=REBAL_BAND) is True        # outside band -- rebalance


def test_custom_stake_amount_falls_back_to_full_size_without_dp():
    """No self.dp wired up (as with a freshly-instantiated strategy outside the
    freqtrade runtime) -> full stake, never a sizing error blocking an entry."""
    s = _strategy()
    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=pd.Timestamp.now(), current_rate=50000.0,
        proposed_stake=100.0, min_stake=10.0, max_stake=1000.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake == 100.0


def test_custom_stake_amount_scales_with_realized_vol():
    """With a fake DataProvider returning a hot-vol dataframe, stake should be
    scaled DOWN below the proposed stake."""
    s = _strategy()
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    rng = np.random.default_rng(42)
    daily_ret = rng.normal(0, 1.50 / np.sqrt(252), size=30)   # ~150% annualized vol
    df = pd.DataFrame({"date": dates, "daily_ret": daily_ret})

    class FakeDP:
        def get_analyzed_dataframe(self, pair, timeframe):
            return df, dates[-1]

    s.dp = FakeDP()
    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=pd.Timestamp.now(), current_rate=50000.0,
        proposed_stake=100.0, min_stake=1.0, max_stake=1000.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert 0 < stake < 100.0
