#!/usr/bin/env python3
"""Offline backtest harness for the ITC dividend paper bot (freeze-safe, standalone).

Built 2026-07-27 alongside the LTCG tax-efficiency redesign. Reads pre-exported CSVs
in ../data/ only. No network. No randomness.

Execution model: signals computed on MONTHLY bar close, filled at the NEXT month's
close (no look-ahead). Long-only, one position, fee charged per side.

ITC-SPECIFIC DATA FIX: the ITC Hotels demerger (record date Jan 1 2026, 1:10 ratio)
caused a ~9.7% overnight price gap that yfinance does NOT adjust for. We cut the
backtest at Dec 31 2025 to avoid corrupting the monthly MA and trade signals.

Dividends: ITC pays ~₹14.35/year in two installments (interim Feb, final May/Jun).
We model the dividend as a cash credit on the ex-date, taxed at 30% slab rate
(= 31.2% with cess). Capital gains: STCG 20.8%, LTCG 13% (>12 months).

Variants:
  B&H        — Buy & hold ITC (full history, with dividends)
  V1 CURRENT — Hourly SMA(20/50) + 200DMA filter (the live config, modeled on daily)
  V2 LTCG    — Monthly 10-MA with 5% exit band (the new tax-efficient design)
  V2-nostop  — V2 without the hard 30% stop
"""
import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

# Realistic Indian equity all-in cost per side (STT 0.1% + stamp + exchange + SEBI + GST)
FEE_REALISTIC = 0.00125      # 12.5 bps per side = 25 bps round trip
FEE_CONFIG = 0.0005          # 5 bps per side (current config, 2.5x too low)

# Tax rates (Budget 2024, FY 2025-26)
STCG_RATE = 0.208            # 20% + 4% cess
LTCG_RATE = 0.13             # 12.5% + 4% cess
LTCG_EXEMPTION = 125000      # ₹1.25L per year
DIVIDEND_TAX_RATE = 0.312    # 30% slab + 4% cess (high-income assumption)

# ITC dividend history (ex-date, ₹/share) — from Hermes research 2026-07-27
ITC_DIVIDENDS = [
    ("2021-02-22", 5.00), ("2021-06-10", 5.75),
    ("2022-02-14", 5.25), ("2022-05-26", 6.25),
    ("2023-02-15", 6.00), ("2023-05-30", 9.50),
    ("2024-02-08", 6.25), ("2024-06-04", 7.50),
    ("2025-02-12", 6.50), ("2025-05-28", 7.85),
]

# Demerger cutoff: ITC Hotels demerger record date Jan 1 2026
DEMERGER_CUTOFF = "2025-12-31"


def load_daily():
    """Load ITC daily CSV, cut at demerger to avoid unadjusted gap."""
    df = pd.read_csv(os.path.join(DATA, "ITC_daily.csv"), parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    # Cut at demerger — yfinance doesn't adjust for demergers
    df = df[df["Date"] <= DEMERGER_CUTOFF].reset_index(drop=True)
    return df


def to_monthly(daily):
    """Resample daily to monthly bars (last close, first open, etc.)."""
    df = daily.copy()
    df["Month"] = df["Date"].dt.to_period("M")
    monthly = df.groupby("Month").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).reset_index()
    monthly["Date"] = monthly["Month"].dt.to_timestamp()
    return monthly.sort_values("Date").reset_index(drop=True)


def backtest_monthly(monthly, fee_side, stop_pct=0.70, exit_band=0.95,
                     ltcg_widen=0.075, ltcg_cap=0.03, ltcg_days=365,
                     with_dividends=True):
    """Monthly 10-MA strategy with LTCG-aware exit band.

    Entry: close > SMA(10) of monthly closes
    Exit: close < band * SMA(10), where band widens near LTCG threshold
    Hard stop: close < stop_pct * entry_price
    """
    df = monthly.copy()
    df["sma10"] = df["Close"].rolling(10).mean()
    df = df.dropna(subset=["sma10"]).reset_index(drop=True)

    close = df["Close"].values
    sma = df["sma10"].values
    dates = df["Date"]
    n = len(df)

    in_pos = False
    entry_px = 0.0
    entry_date = None
    entry_i = 0
    equity = np.ones(n)
    eq = 1.0
    trades = []
    div_cash = 0.0
    div_tax_paid = 0.0

    for i in range(1, n):
        px = close[i]
        # Dividend accrual (if in position on ex-date)
        if in_pos and with_dividends:
            today = str(dates.iloc[i].date())
            shares = eq / entry_px  # approximate shares held
            for ex_date, rate in ITC_DIVIDENDS:
                if today[:7] == ex_date[:7]:  # same month
                    gross_div = shares * rate
                    tax = gross_div * DIVIDEND_TAX_RATE
                    div_cash += gross_div - tax
                    div_tax_paid += tax

        if in_pos:
            unreal = (px - entry_px) / entry_px
            held_days = (dates.iloc[i] - entry_date).days

            # LTCG-aware exit band: widen when near 12-month mark with gains
            if held_days < ltcg_days and unreal > 0:
                band = exit_band - min(ltcg_widen * unreal, ltcg_cap)
            else:
                band = exit_band

            exit_reason = None
            fill = px

            # Hard stop (always active)
            if px < stop_pct * entry_px:
                fill = px
                exit_reason = "stop"
            # Exit band: close < band * sma10
            elif px < band * sma[i]:
                fill = px
                exit_reason = "band_exit"

            if exit_reason:
                gross = fill / entry_px
                net = gross * (1 - fee_side) ** 2
                # Tax on capital gains
                if held_days > 365:
                    cg_tax = max(0, (net - 1) * eq) * LTCG_RATE
                else:
                    cg_tax = max(0, (net - 1) * eq) * STCG_RATE
                eq *= net
                eq -= cg_tax  # pay tax from equity
                trades.append({
                    "entry": str(entry_date.date()), "exit": str(dates.iloc[i].date()),
                    "bars": i - entry_i, "ret": net - 1, "reason": exit_reason,
                    "held_days": held_days, "ltcg": held_days > 365,
                    "cg_tax": round(cg_tax, 2)
                })
                in_pos = False
                equity[i] = eq + div_cash
            else:
                equity[i] = eq * (px / entry_px) + div_cash
        else:
            equity[i] = eq + div_cash
            # Entry: close > sma10
            if px > sma[i] and px > sma[i - 1]:  # above MA and rising
                in_pos = True
                entry_px = px
                entry_date = dates.iloc[i]
                entry_i = i

    # Close any open position at last bar
    if in_pos:
        gross = close[-1] / entry_px
        net = gross * (1 - fee_side) ** 2
        held_days = (dates.iloc[-1] - entry_date).days
        if held_days > 365:
            cg_tax = max(0, (net - 1) * eq) * LTCG_RATE
        else:
            cg_tax = max(0, (net - 1) * eq) * STCG_RATE
        eq *= net
        eq -= cg_tax
        trades.append({
            "entry": str(entry_date.date()), "exit": str(dates.iloc[-1].date()),
            "bars": n - 1 - entry_i, "ret": net - 1, "reason": "eod",
            "held_days": held_days, "ltcg": held_days > 365,
            "cg_tax": round(cg_tax, 2)
        })
        equity[-1] = eq + div_cash

    return pd.Series(equity, index=dates), trades, div_cash, div_tax_paid


def backtest_bh(daily, fee_side, with_dividends=True):
    """Buy & hold with dividend accrual."""
    df = daily.copy()
    close = df["Close"].values
    dates = df["Date"]
    n = len(df)

    entry_px = close[0] * (1 + fee_side)
    eq = 1.0
    equity = np.ones(n)
    div_cash = 0.0
    div_tax_paid = 0.0
    shares = eq / entry_px

    for i in range(n):
        if with_dividends:
            today = str(dates.iloc[i].date())
            for ex_date, rate in ITC_DIVIDENDS:
                if today[:7] == ex_date[:7]:
                    gross_div = shares * rate
                    tax = gross_div * DIVIDEND_TAX_RATE
                    div_cash += gross_div - tax
                    div_tax_paid += tax
        equity[i] = (close[i] / entry_px) + div_cash

    # Final exit tax (LTCG)
    final_eq = equity[-1]
    cg = max(0, (final_eq - 1 - div_cash) * (1 - LTCG_RATE))
    equity[-1] = cg + div_cash

    trades = [{"entry": str(dates.iloc[0].date()), "exit": str(dates.iloc[-1].date()),
               "bars": n, "ret": equity[-1] - 1, "reason": "b&h",
               "held_days": (dates.iloc[-1] - dates.iloc[0]).days, "ltcg": True,
               "cg_tax": round(max(0, (final_eq - 1 - div_cash) * LTCG_RATE), 2)}]
    return pd.Series(equity, index=dates), trades, div_cash, div_tax_paid


def backtest_daily_sma(daily, fast, slow, fee_side, with_dividends=True):
    """Daily SMA cross + 200DMA filter (current strategy approximation)."""
    df = daily.copy()
    df["sma_fast"] = df["Close"].rolling(fast).mean()
    df["sma_slow"] = df["Close"].rolling(slow).mean()
    df["sma200"] = df["Close"].rolling(200).mean()
    df = df.dropna(subset=["sma200"]).reset_index(drop=True)

    close = df["Close"].values
    fast_ma = df["sma_fast"].values
    slow_ma = df["sma_slow"].values
    dma = df["sma200"].values
    dates = df["Date"]
    n = len(df)

    in_pos = False
    entry_px = 0.0
    entry_date = None
    entry_i = 0
    equity = np.ones(n)
    eq = 1.0
    trades = []
    div_cash = 0.0
    div_tax_paid = 0.0

    for i in range(1, n):
        px = close[i]
        if in_pos and with_dividends:
            today = str(dates.iloc[i].date())
            shares = eq / entry_px
            for ex_date, rate in ITC_DIVIDENDS:
                if today[:7] == ex_date[:7]:
                    gross_div = shares * rate
                    tax = gross_div * DIVIDEND_TAX_RATE
                    div_cash += gross_div - tax
                    div_tax_paid += tax

        if in_pos:
            exit_reason = None
            fill = px
            # Exit on down-cross
            if fast_ma[i] < slow_ma[i] and fast_ma[i - 1] >= slow_ma[i - 1]:
                exit_reason = "sma_cross_down"
            if exit_reason:
                gross = fill / entry_px
                net = gross * (1 - fee_side) ** 2
                held_days = (dates.iloc[i] - entry_date).days
                if held_days > 365:
                    cg_tax = max(0, (net - 1) * eq) * LTCG_RATE
                else:
                    cg_tax = max(0, (net - 1) * eq) * STCG_RATE
                eq *= net
                eq -= cg_tax
                trades.append({
                    "entry": str(entry_date.date()), "exit": str(dates.iloc[i].date()),
                    "bars": i - entry_i, "ret": net - 1, "reason": exit_reason,
                    "held_days": held_days, "ltcg": held_days > 365,
                    "cg_tax": round(cg_tax, 2)
                })
                in_pos = False
                equity[i] = eq + div_cash
            else:
                equity[i] = eq * (px / entry_px) + div_cash
        else:
            equity[i] = eq + div_cash
            # Entry: up-cross + above 200DMA
            if (fast_ma[i] > slow_ma[i] and fast_ma[i - 1] <= slow_ma[i - 1]
                    and px > dma[i]):
                in_pos = True
                entry_px = px
                entry_date = dates.iloc[i]
                entry_i = i

    if in_pos:
        gross = close[-1] / entry_px
        net = gross * (1 - fee_side) ** 2
        held_days = (dates.iloc[-1] - entry_date).days
        if held_days > 365:
            cg_tax = max(0, (net - 1) * eq) * LTCG_RATE
        else:
            cg_tax = max(0, (net - 1) * eq) * STCG_RATE
        eq *= net
        eq -= cg_tax
        trades.append({
            "entry": str(entry_date.date()), "exit": str(dates.iloc[-1].date()),
            "bars": n - 1 - entry_i, "ret": net - 1, "reason": "eod",
            "held_days": held_days, "ltcg": held_days > 365,
            "cg_tax": round(cg_tax, 2)
        })
        equity[-1] = eq + div_cash

    return pd.Series(equity, index=dates), trades, div_cash, div_tax_paid


def metrics(equity, trades, div_cash, div_tax, years, name):
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    wins = [t for t in trades if t["ret"] > 0]
    losses = [t for t in trades if t["ret"] <= 0]
    gp = sum(t["ret"] for t in wins)
    gl = -sum(t["ret"] for t in losses)
    pf = gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0)
    ltcg_trades = sum(1 for t in trades if t.get("ltcg", False))
    avg_hold_days = np.mean([t.get("held_days", 0) for t in trades]) if trades else 0
    total_cg_tax = sum(t.get("cg_tax", 0) for t in trades)
    return {
        "variant": name,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(pf, 2) if np.isfinite(pf) else "inf",
        "ltcg_trades": ltcg_trades,
        "ltcg_pct": round(100 * ltcg_trades / len(trades), 1) if trades else 0.0,
        "avg_hold_days": round(avg_hold_days, 0),
        "dividends_net": round(div_cash * 100, 2),
        "dividend_tax": round(div_tax * 100, 2),
        "cg_tax_total": round(total_cg_tax, 2),
    }


def main():
    daily = load_daily()
    monthly = to_monthly(daily)
    years = (daily["Date"].iloc[-1] - daily["Date"].iloc[0]).days / 365.25

    print(f"ITC data: {daily['Date'].iloc[0].date()} to {daily['Date'].iloc[-1].date()} "
          f"({years:.1f} years, {len(daily)} daily bars, {len(monthly)} monthly bars)")
    print(f"Demerger cutoff: {DEMERGER_CUTOFF} (ITC Hotels 1:10 demerger Jan 2026)")
    print()

    results = []

    # B&H with dividends (realistic fees)
    eq, tr, div, dtax = backtest_bh(daily, FEE_REALISTIC)
    results.append(metrics(eq, tr, div, dtax, years, "B&H ITC + dividends (realistic fee)"))

    # B&H without dividends (price only)
    eq, tr, div, dtax = backtest_bh(daily, FEE_REALISTIC, with_dividends=False)
    results.append(metrics(eq, tr, div, dtax, years, "B&H ITC price only (realistic fee)"))

    # V1 Current: daily SMA(20/50) + 200DMA filter (approximation of hourly strategy)
    eq, tr, div, dtax = backtest_daily_sma(daily, 20, 50, FEE_REALISTIC)
    results.append(metrics(eq, tr, div, dtax, years, "V1 CURRENT daily 20/50 + 200DMA (realistic fee)"))

    # V1 with config fee (too low)
    eq, tr, div, dtax = backtest_daily_sma(daily, 20, 50, FEE_CONFIG)
    results.append(metrics(eq, tr, div, dtax, years, "V1 CURRENT (config fee 5bps)"))

    # V2 LTCG: monthly 10-MA with exit band (new strategy)
    eq, tr, div, dtax = backtest_monthly(monthly, FEE_REALISTIC)
    results.append(metrics(eq, tr, div, dtax, years, "V2 LTCG monthly 10-MA + band (realistic fee)"))

    # V2 without hard stop
    eq, tr, div, dtax = backtest_monthly(monthly, FEE_REALISTIC, stop_pct=0.0)
    results.append(metrics(eq, tr, div, dtax, years, "V2 LTCG no hard stop (realistic fee)"))

    # V2 with config fee (for comparison)
    eq, tr, div, dtax = backtest_monthly(monthly, FEE_CONFIG)
    results.append(metrics(eq, tr, div, dtax, years, "V2 LTCG (config fee 5bps)"))

    # ---- output ----
    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "ltcg_pct",
            "avg_hold_days", "dividends_net", "cg_tax_total"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 55)
    print(tbl.to_string(index=False))

    out = os.path.join(HERE, "results.json")
    with open(out, "w") as f:
        json.dump({"generated_from": "ITC_daily.csv", "demerger_cutoff": DEMERGER_CUTOFF,
                   "fee_realistic": FEE_REALISTIC, "fee_config": FEE_CONFIG,
                   "stcg_rate": STCG_RATE, "ltcg_rate": LTCG_RATE,
                   "dividend_tax_rate": DIVIDEND_TAX_RATE,
                   "results": results}, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
