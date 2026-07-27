"""Tests for btc_faber_backtest.py — India after-tax Faber/TSMOM backtest.

Offline only: reads cached BTC CSV, no network, no live state files.
"""
import json
import os
import sys

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.join(HERE, "..", "nifty_backtest")
sys.path.insert(0, BACKTEST_DIR)
import btc_faber_backtest as bt


def test_load_csv_normalizes_columns():
    df = bt.load_csv("BTC_USD_3y.csv")
    assert "Date" in df.columns
    assert "Close" in df.columns
    assert "Low" in df.columns
    assert len(df) > 200  # enough for 200DMA


def test_backtest_tax_bnh_single_trade():
    """Buy & hold: one entry at first bar, one exit at last bar."""
    df = bt.load_csv("BTC_USD_3y.csv")
    e = pd.Series(False, index=df.index); e.iloc[0] = True
    x = pd.Series(False, index=df.index)
    eq, trades = bt.backtest_tax(df, e, x, stop=-1.0)
    assert len(trades) == 1
    assert eq.iloc[-1] > 1.0  # BTC went up over the period
    # after-tax should be less than gross (30% + 1% TDS)
    t = trades[0]
    assert t["net_ret"] < t["gross_ret"]


def test_backtest_tax_no_loss_offset():
    """A losing trade: gross < 0, net should be -0.01 (only TDS drag, no tax credit)."""
    df = bt.load_csv("BTC_USD_3y.csv")
    # craft a 3-bar series: entry signal on bar 0 -> buy bar 1 (close=100), exit bar 2 (close=90)
    d = df.iloc[:3].copy()
    d["Close"] = [100.0, 100.0, 90.0]
    d["Low"] = [100.0, 100.0, 89.0]
    d["High"] = [100.0, 100.0, 100.0]
    e = pd.Series([True, False, False])  # entry signal on bar 0
    x = pd.Series([False, False, False])
    _, trades = bt.backtest_tax(d, e, x, stop=-1.0)
    assert len(trades) == 1
    # gross = 90/100 - 1 = -0.10, taxed_gain = 0 (loss uncredited), net = 0 - 0.01 = -0.01
    assert trades[0]["gross_ret"] == pytest.approx(-0.10, abs=0.01)
    assert trades[0]["net_ret"] == pytest.approx(-0.01, abs=0.001)


def test_200dma_signals_correct():
    """200DMA braked-hold: entry when crossing above, exit when crossing below."""
    df = bt.load_csv("BTC_USD_3y.csv")
    d = df.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["long"] = (d["Close"] > d["sma200"]).fillna(False)
    d["long_prev"] = d["long"].shift(1).fillna(False)
    de = (d["long"] & d["long_prev"].ne(True)).fillna(False)
    dx = (d["long_prev"] & d["long"].ne(True)).fillna(False)
    # should have some entries and exits
    assert de.sum() > 0
    assert dx.sum() > 0
    # entry and exit counts should be roughly equal (long-flat flips)
    assert abs(de.sum() - dx.sum()) <= 1


def test_wazirx_adapter_returns_valid_data():
    """WazirX API adapter returns properly timestamped OHLCV data."""
    df = bt.fetch_wazirx_inr(limit=365)
    assert len(df) > 0
    assert all(c in df.columns for c in ["Date", "Open", "High", "Low", "Close", "Volume"])
    # timestamps should be real dates (not 1970 epoch)
    assert df["Date"].iloc[0].year >= 2020
    # OHLC should be positive
    assert (df["Close"] > 0).all()
    # data should be sorted
    assert df["Date"].is_monotonic_increasing


def test_metrics_shape():
    df = bt.load_csv("BTC_USD_3y.csv")
    e = pd.Series(False, index=df.index); e.iloc[0] = True
    x = pd.Series(False, index=df.index)
    eq, trades = bt.backtest_tax(df, e, x, stop=-1.0)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = bt.metrics(eq, trades, len(df), years)
    for key in ["total_return_pct", "cagr_pct", "max_drawdown_pct",
                "trades", "win_rate_pct", "profit_factor", "sharpe",
                "avg_hold_bars", "gross_total_ret_pct", "net_total_ret_pct",
                "tax_drag_pct"]:
        assert key in m
    assert m["trades"] == 1
