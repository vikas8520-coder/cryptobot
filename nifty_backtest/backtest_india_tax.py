#!/usr/bin/env python3
"""backtest_india_tax.py -- CLI wrapper around btc_faber_backtest's after-tax engine,
scoped to the strategies actually shipped as freqtrade files (BrakedHold /
BrakedHoldV2), so "does the India after-tax number look right for what's actually
running" is one command instead of editing btc_faber_backtest.py's main().

FREEZE-SAFE: BTC/USDT reads the cached CSV in ../user_data/backtest_equity/ (no
network ever); BTC/INR goes through inr_data.py's cached fetch chain
(wazirx -> coindcx -> coingecko), which only touches the network on a cold cache.

After-tax formula and the backtest engine itself (backtest_tax, metrics) live in
btc_faber_backtest.py -- this file only derives entry/exit signals per strategy
and adds the DCA-into-BTC benchmark for comparison.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import btc_faber_backtest as bt  # noqa: E402  (needs sys.path set up first)
import inr_data  # noqa: E402


def sma200_signals(df, exit_band=1.0):
    """200DMA braked-hold entry/exit series. exit_band<1.0 widens the exit brake
    (e.g. 0.98 = BrakedHoldV2's 2% band) to cut whipsaw churn; 1.0 = BrakedHold's
    raw cross (no band)."""
    d = df.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["long"] = (d["Close"] > d["sma200"]).fillna(False)
    d["long_prev"] = d["long"].shift(1).fillna(False)
    # exit uses the (possibly banded) brake independently of the entry condition,
    # so a position can be open while close is between sma200*exit_band and sma200
    d["exit_now"] = (d["Close"] < d["sma200"] * exit_band).fillna(False)
    d["exit_now_prev"] = d["exit_now"].shift(1).fillna(False)
    entry = (d["long"] & d["long_prev"].ne(True)).fillna(False)
    exit_ = (d["exit_now"] & d["exit_now_prev"].ne(True)).fillna(False)
    return entry, exit_


STRATEGIES = {
    # (exit_band, description) -- entry is always close > sma200 for this family
    "BrakedHold": 1.0,
    "BrakedHoldV2": 0.98,
}


def dca_benchmark(df, interval_days=30, tds=bt.TDS, tax_rate=bt.TAX_RATE):
    """Dollar-cost-average into BTC every `interval_days`, hold to the end, sell
    once. Cost basis is the harmonic-mean price (equal $ per tranche); tax applies
    once, on the single final sale, same after-tax formula as backtest_tax().

    Returns (equity: pd.Series indexed like df["Date"], trades: list[dict]) so it
    plugs straight into btc_faber_backtest.metrics() for an apples-to-apples table.
    """
    close = df["Close"].values
    dates = df["Date"]
    n = len(df)
    qty = 0.0
    invested = 0.0
    equity = np.ones(n)
    amount_per_tranche = 1.0  # unit capital; equity is expressed as a multiple of it
    for i in range(n):
        if i % interval_days == 0:
            qty += amount_per_tranche / close[i]
            invested += amount_per_tranche
        equity[i] = (qty * close[i]) / invested if invested > 0 else 1.0

    gross_ret = (qty * close[-1]) / invested - 1 if invested > 0 else 0.0
    taxed_gain = max(gross_ret, 0) * (1 - tax_rate)
    net_ret = taxed_gain - tds
    equity[-1] = 1.0 + net_ret

    trades = [{
        "entry": str(dates.iloc[0]), "exit": str(dates.iloc[-1]),
        "bars": n - 1, "gross_ret": gross_ret, "net_ret": net_ret,
        "reason": "dca_eod",
    }]
    return pd.Series(equity, index=dates), trades


def load_pair_data(pair, days):
    """BTC/USDT -> cached USD CSV, trimmed to the last `days` rows.
    BTC/INR -> inr_data's cached fetch chain (wazirx, falls back to coindcx then
    coingecko internally on a cold/unreachable cache).
    NIFTYBEES -> cached Indian equity CSV in ../data/ (daily)."""
    if pair.upper() in ("BTC/USDT", "BTC/USD", "BTCUSDT"):
        df = bt.load_csv("BTC_USD_3y.csv")
        return df.tail(days).reset_index(drop=True)
    if pair.upper() in ("BTC/INR", "BTCINR"):
        df = inr_data.fetch_wazirx_inr(limit=days)
        if len(df) < 200:
            df = inr_data.fetch_coindcx_inr(limit=days)
        return df.tail(days).reset_index(drop=True)
    if pair.upper() in ("NIFTYBEES", "NIFTYBEES/INR", "NIFTYBEES.NS"):
        # NIFTYBEES data lives in ../data/ (not user_data/backtest_equity/)
        import os as _os
        npath = _os.path.join(HERE, "..", "data", "NIFTYBEES_daily.csv")
        df = pd.read_csv(npath, parse_dates=["Date"])
        df = df.sort_values("Date").reset_index(drop=True)
        df.columns = [c.capitalize() for c in df.columns]
        return df.tail(days).reset_index(drop=True)
    raise ValueError(f"unsupported pair: {pair!r} (supported: BTC/USDT, BTC/INR, NIFTYBEES)")


def run(strategy, pair, days):
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r} (known: {list(STRATEGIES)})")
    df = load_pair_data(pair, days)
    if len(df) < 200:
        raise ValueError(f"only {len(df)} bars of {pair} data -- need >=200 for the SMA")

    exit_band = STRATEGIES[strategy]
    entry, exit_ = sma200_signals(df, exit_band=exit_band)
    eq, trades = bt.backtest_tax(df, entry, exit_, stop=-1.0)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = bt.metrics(eq, trades, len(df), max(years, 1e-6))
    m["variant"] = f"{strategy} ({pair}, after-tax)"

    dca_eq, dca_trades = dca_benchmark(df)
    dca_m = bt.metrics(dca_eq, dca_trades, len(df), max(years, 1e-6))
    dca_m["variant"] = f"DCA-into-BTC benchmark ({pair}, after-tax)"

    return {"strategy": m, "dca_benchmark": dca_m, "bars": len(df), "pair": pair,
            "date_range": [str(df["Date"].iloc[0].date()), str(df["Date"].iloc[-1].date())]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", default="BrakedHoldV2", choices=list(STRATEGIES))
    ap.add_argument("--pair", default="BTC/USDT")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--out", default=os.path.join(HERE, "backtest_india_tax_results.json"))
    args = ap.parse_args()

    result = run(args.strategy, args.pair, args.days)

    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct", "trades",
            "win_rate_pct", "profit_factor", "sharpe", "avg_hold_bars",
            "gross_total_ret_pct", "net_total_ret_pct", "tax_drag_pct"]
    tbl = pd.DataFrame([result["strategy"], result["dca_benchmark"]])[cols]
    pd.set_option("display.width", 220)
    print(f"{args.strategy} vs DCA-into-BTC | {args.pair} | "
          f"{result['date_range'][0]} -> {result['date_range'][1]} ({result['bars']} bars)")
    print(tbl.to_string(index=False))

    try:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote {args.out}")
    except Exception as e:
        print(f"\n(could not write {args.out}: {e})")


if __name__ == "__main__":
    main()
