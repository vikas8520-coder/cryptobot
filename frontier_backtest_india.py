#!/usr/bin/env python3
"""
frontier_backtest_india.py — does the diversification drawdown benefit survive with
INDIA-TRADEABLE instruments (Zerodha/NSE), not the US ETFs?

The US frontier test used SPY / GLD / TLT — which an Indian resident can only reach via
LRS (TCS + foreign-asset reporting). The tradeable-from-India version swaps in NSE ETFs:
  Stocks → NIFTYBEES     Gold → GOLDBEES     Bonds → a long-duration gilt/bond ETF
Everything is put in INR (crypto converted via USD/INR) so it's a fair same-currency
portfolio for an Indian investor, and idle cash earns the INR risk-free (~6%, not 4.5%).

CRITICAL: Indian assets are NOT the same as the US ones — NIFTY≠S&P500, an Indian gilt≠
US 20y Treasuries (different rates AND currency). Diversification depends on CORRELATION,
so the 16%-DD US result does NOT automatically transfer. This measures whether it holds.

No lookahead (braked_returns shifts the signal). NSE ETFs forward-filled onto the crypto
daily calendar (markets closed = flat). Data via yfinance (.NS tickers).
"""
from __future__ import annotations
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

from regime_hold_backtest import daily_closes, basket, DAYS_PER_YEAR
from frontier_backtest import braked_returns, eq_metrics

warnings.filterwarnings("ignore")

CASH_INR = 0.06                 # INR risk-free (T-bill / liquid fund) ~6%, vs US 4.5%
STOCKS = "NIFTYBEES.NS"
GOLD = "GOLDBEES.NS"
# long-duration Indian gilt ETFs — LTGILTBEES (Nippon Long-Term Gilt, real G-Sec, the
# true TLT analog) has full 2020+ history; Bharat Bond ETFs only go back to 2025 on yfinance.
# Pick the candidate with the LONGEST history so we cover the full crypto cycle.
BOND_CANDIDATES = ["LTGILTBEES.NS", "NETFLTGILT.NS", "SETFGILT.NS",
                   "EBBETF0433.NS", "EBBETF0430.NS"]


def to_ns(ix):
    """Normalize any index to tz-naive, date-normalized, nanosecond resolution so
    pandas 2.x reindex/multiply can compare them (it preserves unit + tz otherwise)."""
    ix = pd.DatetimeIndex(pd.to_datetime(ix))
    if ix.tz is not None:
        ix = ix.tz_convert("UTC").tz_localize(None)
    return ix.normalize().as_unit("ns")


def _series(df, ticker):
    """Pull one ticker's Close as a tz-naive, ns-normalized series."""
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]
        close = close[ticker] if ticker in close.columns else close.iloc[:, 0]
    else:
        close = df["Close"]
    close = close.dropna()
    close.index = to_ns(close.index)
    return close


def fetch(ticker, index):
    df = yf.download(ticker, start="2020-10-01", end="2026-07-01",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None
    s = _series(df, ticker)
    if s.dropna().shape[0] < 300:
        return None
    return s.reindex(index, method="ffill")


def main():
    closes = daily_closes()
    closes.index = to_ns(closes.index)
    idx = closes.index
    crypto_usd = basket(closes)
    print(f"Crypto daily: {idx.min().date()} -> {idx.max().date()} ({len(idx)} days)")

    # USD/INR to put crypto in INR terms for an Indian holder
    fx = fetch("INR=X", idx)
    if fx is None:
        print("no USD/INR data — aborting"); return
    crypto_inr = (crypto_usd * fx).dropna()      # crypto value in INR for an Indian holder

    stocks = fetch(STOCKS, idx)
    gold = fetch(GOLD, idx)
    bond = None
    bond_name = None
    for t in BOND_CANDIDATES:
        bond = fetch(t, idx)
        if bond is not None:
            bond_name = t
            break

    have = {"NIFTYBEES": stocks is not None, "GOLDBEES": gold is not None,
            f"bond({bond_name})": bond is not None}
    print("data availability:", have)
    if not (stocks is not None and gold is not None and bond is not None):
        print("⚠️ missing an India sleeve — cannot run the 4-sleeve test")
        return

    # align all to a common date range (India ETFs may start later)
    df = pd.DataFrame({"crypto": crypto_inr, "stocks": stocks, "gold": gold, "bond": bond}).dropna()
    print(f"India-sleeve overlap: {df.index.min().date()} -> {df.index.max().date()} ({len(df)} days)")

    sleeves = {k: braked_returns(df[k], CASH_INR)[0] for k in df.columns}
    R = pd.DataFrame(sleeves).dropna()

    rows = []
    co = eq_metrics(R["crypto"], braked_returns(df["crypto"], CASH_INR)[2])
    rows.append(("crypto-only braked (INR, +6% cash)", co))
    for k, label in (("stocks", "NIFTYBEES"), ("gold", "GOLDBEES"), ("bond", f"bond {bond_name}")):
        m = eq_metrics(R[k], braked_returns(df[k], CASH_INR)[2])
        rows.append((f"  sleeve: {label} braked", m))
    div = R.mean(axis=1)
    dm = eq_metrics(div, 0.5)
    rows.append(("DIVERSIFIED 4-sleeve INDIA (equal wt)", dm))

    # correlation of the sleeves' daily returns — the crux of whether diversification works
    corr = R.corr()

    print("\n" + "=" * 90)
    print("INDIA-TRADEABLE DIVERSIFIED BRAKE — INR, braked + 6% cash, no lookahead")
    print("=" * 90)
    print(f"{'strategy':<40}{'return':>9}{'CAGR':>8}{'maxDD':>8}{'Calmar':>8}{'%inMkt':>8}")
    print("-" * 90)
    for name, m in rows:
        print(f"{name:<40}{m['total']*100:>8.0f}%{m['cagr']*100:>7.1f}%"
              f"{m['maxdd']*100:>7.0f}%{m['calmar']:>8.2f}{m['tim']*100:>7.0f}%")
    print("-" * 90)
    print("\nSleeve return CORRELATIONS (lower = better diversification):")
    print(corr.round(2).to_string())
    print("\nCompare to the US version: crypto-only 51% DD / 0.43 Calmar → US-diversified 16% / 0.70.")
    print("The India benefit holds only if the diversified maxDD lands well below crypto-only's.")


if __name__ == "__main__":
    main()
