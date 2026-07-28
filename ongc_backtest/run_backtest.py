#!/usr/bin/env python3
"""Offline backtest for ONGC — monthly 200DMA trend strategy.

Built 2026-07-27. Uses auto_adjust=True data (splits + dividends reinvested in price).
Dividend tax modeled as a continuous drag (30% of yield) while in position, since
the adjusted price already includes gross dividends.

Tax model: at ₹9,656 capital, LTCG exemption (₹1.25L/yr) means LTCG is effectively 0%.
STCG is 20.8%. Dividends taxed at 30% slab.
"""
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

FEE_REALISTIC = 0.00125
STCG_RATE = 0.208
LTCG_RATE = 0.0              # effectively 0 at ₹9,656 capital
DIVIDEND_TAX = 0.30
DIVIDEND_YIELD = 0.055       # ONGC ~5.5% yield
DIVIDEND_DRAG = DIVIDEND_YIELD * DIVIDEND_TAX  # 1.65%/yr continuous drag while in position

START = "2006-01-01"         # avoid 2005 split artifact


def load_daily():
    df = pd.read_csv(os.path.join(DATA, "ONGC_daily.csv"), parse_dates=["Date"])
    df = df[df["Date"] >= START].reset_index(drop=True)
    return df.sort_values("Date").reset_index(drop=True)


def backtest_bh(daily, fee_side):
    """Buy & hold. Adjusted price includes dividends; subtract dividend tax drag."""
    close = daily["Close"].values
    dates = daily["Date"]
    n = len(daily)
    entry_px = close[0] * (1 + fee_side)
    equity = np.ones(n)
    for i in range(n):
        years_held = i / 252.0  # approx trading days per year
        div_drag = DIVIDEND_DRAG * years_held
        equity[i] = (close[i] / entry_px) * (1 - div_drag)
    return pd.Series(equity, index=dates), []


def backtest_monthly_trend(daily, fee_side, sma_period=200):
    """Monthly 200DMA trend: own when close > SMA at month-end, else cash.

    Key insight from Claude: monthly cadence IS the noise filter.
    No exit band, no stop loss (stops convert LTCG-0% to STCG-20.8%).
    Dividend tax modeled as drag while in position.
    """
    df = daily.copy()
    df["sma"] = df["Close"].rolling(sma_period).mean()
    df = df.dropna(subset=["sma"]).reset_index(drop=True)

    close = df["Close"].values
    sma = df["sma"].values
    dates = df["Date"]
    n = len(df)

    df["Month"] = df["Date"].dt.to_period("M")
    month_ends = df.groupby("Month").tail(1)
    # Signal computed at month-end, acted on FIRST day of NEXT month (no look-ahead)
    month_target = {}
    prev_months = list(month_ends["Month"])
    for idx, row in month_ends.iterrows():
        month = str(row["Month"])
        target = "LONG" if row["Close"] > row["sma"] else "FLAT"
        # This target applies to the NEXT month's first trading day
        month_target[month] = target

    in_pos = False
    entry_px = 0.0
    entry_date = None
    entry_i = 0
    equity = np.ones(n)
    eq = 1.0
    trades = []
    prev_month = None
    pending_target = None    # signal from previous month-end, acted on this month
    days_in_pos = 0

    for i in range(n):
        px = close[i]
        today = dates.iloc[i]
        month = str(df["Month"].iloc[i])

        if month != prev_month:
            # Act on the PREVIOUS month's end signal (no look-ahead)
            if pending_target is not None:
                if pending_target == "LONG" and not in_pos:
                    in_pos = True
                    entry_px = px * (1 + fee_side)
                    entry_date = today
                    entry_i = i
                    days_in_pos = 0
                elif pending_target == "FLAT" and in_pos:
                    gross = px * (1 - fee_side) / entry_px
                    held_days = (today - entry_date).days
                    div_drag = DIVIDEND_DRAG * (days_in_pos / 252.0)
                    gross *= (1 - div_drag)
                    tax_rate = LTCG_RATE if held_days > 365 else STCG_RATE
                    cg_tax = max(0, (gross - 1) * eq) * tax_rate
                    eq *= gross
                    eq -= cg_tax
                    trades.append({
                        "entry": str(entry_date.date()), "exit": str(today.date()),
                        "held_days": held_days, "ret": gross - 1,
                        "ltcg": held_days > 365, "cg_tax": round(cg_tax, 4)
                    })
                    in_pos = False
            # Save this month's end signal for next month
            pending_target = month_target.get(month, "FLAT")
            prev_month = month

        if in_pos:
            days_in_pos += 1
            equity[i] = eq * (px / entry_px)
        else:
            equity[i] = eq

    if in_pos:
        px = close[-1]
        gross = px * (1 - fee_side) / entry_px
        held_days = (dates.iloc[-1] - entry_date).days
        div_drag = DIVIDEND_DRAG * (days_in_pos / 252.0)
        gross *= (1 - div_drag)
        tax_rate = LTCG_RATE if held_days > 365 else STCG_RATE
        cg_tax = max(0, (gross - 1) * eq) * tax_rate
        eq *= gross
        eq -= cg_tax
        trades.append({
            "entry": str(entry_date.date()), "exit": str(dates.iloc[-1].date()),
            "held_days": held_days, "ret": gross - 1,
            "ltcg": held_days > 365, "cg_tax": round(cg_tax, 4)
        })
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def backtest_current(daily, fee_side, fast=20, slow=50, dma=200):
    """Current strategy: daily SMA(20/50) cross + 200DMA filter."""
    df = daily.copy()
    df["sma_fast"] = df["Close"].rolling(fast).mean()
    df["sma_slow"] = df["Close"].rolling(slow).mean()
    df["sma200"] = df["Close"].rolling(dma).mean()
    df = df.dropna(subset=["sma200"]).reset_index(drop=True)

    close = df["Close"].values
    fast_ma = df["sma_fast"].values
    slow_ma = df["sma_slow"].values
    dma_arr = df["sma200"].values
    dates = df["Date"]
    n = len(df)

    in_pos = False
    entry_px = 0.0
    entry_date = None
    equity = np.ones(n)
    eq = 1.0
    trades = []
    days_in_pos = 0

    for i in range(1, n):
        px = close[i]
        if in_pos:
            days_in_pos += 1
            if fast_ma[i] < slow_ma[i] and fast_ma[i-1] >= slow_ma[i-1]:
                gross = px * (1 - fee_side) / entry_px
                held_days = (dates.iloc[i] - entry_date).days
                div_drag = DIVIDEND_DRAG * (days_in_pos / 252.0)
                gross *= (1 - div_drag)
                tax_rate = LTCG_RATE if held_days > 365 else STCG_RATE
                cg_tax = max(0, (gross - 1) * eq) * tax_rate
                eq *= gross
                eq -= cg_tax
                trades.append({
                    "entry": str(entry_date.date()), "exit": str(dates.iloc[i].date()),
                    "held_days": held_days, "ret": gross - 1,
                    "ltcg": held_days > 365, "cg_tax": round(cg_tax, 4)
                })
                in_pos = False
                equity[i] = eq
            else:
                equity[i] = eq * (px / entry_px)
        else:
            equity[i] = eq
            if (fast_ma[i] > slow_ma[i] and fast_ma[i-1] <= slow_ma[i-1]
                    and px > dma_arr[i]):
                in_pos = True
                entry_px = px * (1 + fee_side)
                entry_date = dates.iloc[i]
                days_in_pos = 0

    if in_pos:
        px = close[-1]
        gross = px * (1 - fee_side) / entry_px
        held_days = (dates.iloc[-1] - entry_date).days
        div_drag = DIVIDEND_DRAG * (days_in_pos / 252.0)
        gross *= (1 - div_drag)
        tax_rate = LTCG_RATE if held_days > 365 else STCG_RATE
        cg_tax = max(0, (gross - 1) * eq) * tax_rate
        eq *= gross
        eq -= cg_tax
        trades.append({
            "entry": str(entry_date.date()), "exit": str(dates.iloc[-1].date()),
            "held_days": held_days, "ret": gross - 1,
            "ltcg": held_days > 365, "cg_tax": round(cg_tax, 4)
        })
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def metrics(equity, trades, years, name):
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    wins = [t for t in trades if t["ret"] > 0]
    ltcg_count = sum(1 for t in trades if t.get("ltcg", False))
    avg_hold = np.mean([t.get("held_days", 0) for t in trades]) if trades else 0
    trades_per_yr = len(trades) / years if years > 0 else 0
    return {
        "variant": name,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_dd_pct": round(dd * 100, 2),
        "trades": len(trades),
        "trades_per_yr": round(trades_per_yr, 2),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0,
        "ltcg_pct": round(100 * ltcg_count / len(trades), 1) if trades else 0,
        "avg_hold_days": round(avg_hold, 0),
    }


def main():
    daily = load_daily()
    years = (daily["Date"].iloc[-1] - daily["Date"].iloc[0]).days / 365.25

    print(f"ONGC data: {daily['Date'].iloc[0].date()} to {daily['Date'].iloc[-1].date()} "
          f"({years:.1f} years)")
    print(f"Tax: STCG={STCG_RATE*100}%, LTCG={LTCG_RATE*100}% (effective), "
          f"Div drag={DIVIDEND_DRAG*100:.2f}%/yr")
    print(f"Fee: {FEE_REALISTIC*100}% per side")
    print()

    results = []

    windows = [
        ("20y", "2006-01-01", "2026-07-27"),
        ("15y", "2011-01-01", "2026-07-27"),
        ("10y", "2016-01-01", "2026-07-27"),
        ("5y", "2021-01-01", "2026-07-27"),
    ]

    for label, start, end in windows:
        d = daily[(daily["Date"] >= start) & (daily["Date"] <= end)].reset_index(drop=True)
        y = (d["Date"].iloc[-1] - d["Date"].iloc[0]).days / 365.25

        eq, tr = backtest_bh(d, FEE_REALISTIC)
        results.append(metrics(eq, tr, y, f"B&H ({label})"))

        eq, tr = backtest_current(d, FEE_REALISTIC)
        results.append(metrics(eq, tr, y, f"V1 CURRENT SMA20/50+200DMA ({label})"))

        eq, tr = backtest_monthly_trend(d, FEE_REALISTIC)
        results.append(metrics(eq, tr, y, f"V2 Monthly 200DMA trend ({label})"))

    cols = ["variant", "total_return_pct", "cagr_pct", "max_dd_pct",
            "trades", "trades_per_yr", "win_rate_pct", "ltcg_pct", "avg_hold_days"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 45)
    print(tbl.to_string(index=False))

    out = os.path.join(HERE, "results.json")
    with open(out, "w") as f:
        json.dump({"data": "ONGC_daily.csv", "start": START,
                   "fee": FEE_REALISTIC, "stcg": STCG_RATE, "ltcg": LTCG_RATE,
                   "div_drag": DIVIDEND_DRAG, "results": results}, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
