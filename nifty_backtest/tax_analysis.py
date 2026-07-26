#!/usr/bin/env python3
"""
tax_analysis.py — after-tax reality check for the Nifty paper bot (freeze-safe).

WHY: nifty_backtest/run_backtest.py proved V3-nostop (1d 200DMA braked-hold, no
hard stop) is the best pre-tax variant (+170% / CAGR 5.82% / PF 2.63 / Sharpe 0.54
over 2009-2026). But the GO_LIVE_CHECKLIST's honest open question is whether that
edge survives INDIA'S TAX REGIME, which is brutal for any churny system:
  - 30% FLAT tax on gains (no slab benefit).
  - 1% TDS deducted on EVERY SELL (gross sell value), win or lose.
  - NO loss offset — a losing trade's loss cannot reduce another's tax.
This script layers exactly that on top of the SAME execution engine run_backtest.py
uses, so pre-tax numbers are byte-for-byte comparable and the tax delta is a clean
read on top. Read-only: reads ../data CSVs, touches no bot/config/strategy.

CROSS-CHECK built in: when TAX_RATE/G/TDS are all 0, backtest_tax() must reproduce
run_backtest.py's published pre-tax figures (V3-nostop total 170.03 etc.). If the
zero-tax run disagrees, the extension is broken — fail loudly.

MODEL (long-only, per closed trade, position equity E before exit, gross = fill/entry):
  net      = gross * (1-fee)^2                 # broker fee both sides
  tds      = TDS * gross                       # 1% of SELL VALUE, charged every sell
  gain_tax = TAX_RATE * max(net - 1, 0)        # 30% on gains, NO loss offset
  equity  *= net - tds - gain_tax              # post-tax multiplier for this trade
Same rule applied to the single B&H trade at the end of the window.
"""
import os
import json
import numpy as np
import pandas as pd

import run_backtest as rb   # reuse load_csv / build_daily_sma200_gate / sma_cross_signals

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

TAX_RATE = 0.30     # 30% flat on gains
TDS = 0.01          # 1% TDS on every sell (gross sell value)

FEE_LOW = rb.FEE_LOW
STOP = rb.STOP


def backtest_tax(df, entry_sig, exit_sig, fee_side, stop=STOP, max_hold_bars=0,
                 gate=None, tax_on=True):
    """Faithful copy of run_backtest.backtest() with an optional post-tax exit
    multiplier. tax_on=False (all tax params 0) MUST reproduce backtest() exactly."""
    close = df["Close"].values
    low = df["Low"].values
    dates = df["Date"]
    days = dates.dt.date.values
    n = len(df)
    tr = TAX_RATE if tax_on else 0.0
    tds = TDS if tax_on else 0.0

    in_pos = False
    entry_px = 0.0
    entry_i = 0
    equity = np.ones(n)
    eq = 1.0
    trades = []
    pos_bars = 0

    ent = entry_sig.values
    exi = exit_sig.values

    for i in range(1, n):
        px = close[i]
        if in_pos:
            pos_bars += 1
            eq_now = eq * (px / entry_px)
            exit_reason = None
            fill = px
            stop_px = entry_px * (1 + stop)
            if low[i] <= stop_px:
                fill = stop_px
                exit_reason = "stop"
            elif max_hold_bars and (i - entry_i) >= max_hold_bars:
                fill = px
                exit_reason = "timeout"
            elif exi[i - 1]:
                fill = px
                exit_reason = "signal"
            if exit_reason:
                gross = fill / entry_px
                net = gross * (1 - fee_side) ** 2
                if tax_on:
                    multiplier = net - tds * gross - (tr * (net - 1) if net > 1 else 0.0)
                else:
                    multiplier = net
                eq *= multiplier
                trades.append({"entry": str(dates[entry_i]), "exit": str(dates[i]),
                               "bars": i - entry_i, "ret": multiplier - 1,
                               "reason": exit_reason})
                in_pos = False
                equity[i] = eq
            else:
                equity[i] = eq_now
        else:
            equity[i] = eq
            if ent[i - 1]:
                ok = True
                if gate is not None:
                    g = gate["gate"].get(days[i], np.nan)
                    ok = bool(g) if g == g else False
                if ok:
                    in_pos = True
                    entry_px = px
                    entry_i = i

    if in_pos:
        gross = close[-1] / entry_px
        net = gross * (1 - fee_side) ** 2
        multiplier = (net - tds * gross - (tr * (net - 1) if net > 1 else 0.0)) if tax_on else net
        eq *= multiplier
        trades.append({"entry": str(dates[entry_i]), "exit": str(dates.iloc[-1]),
                       "bars": n - 1 - entry_i, "ret": multiplier - 1, "reason": "eod"})
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def metrics(equity, trades, bars_in_market, n_bars, years):
    return rb.metrics(equity, trades, bars_in_market, n_bars, years)


def run_variant(name, df, entry, exit_, fee_side, gate=None, max_hold_bars=0,
                stop=STOP, tax_on=True):
    eq, trades = backtest_tax(df, entry, exit_, fee_side, stop=stop,
                              max_hold_bars=max_hold_bars, gate=gate, tax_on=tax_on)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    bars_in = sum(t["bars"] for t in trades)
    m = metrics(eq, trades, bars_in, len(df), years)
    m["variant"] = name
    return m


def main():
    daily = rb.load_csv("NIFTYBEES_daily.csv")
    hourly = rb.load_csv("NIFTYBEES_hourly.csv")
    gate = rb.build_daily_sma200_gate(daily)
    years_full = (daily["Date"].iloc[-1] - daily["Date"].iloc[0]).days / 365.25

    out = []

    # B&H (single trade, full history)
    bh_entry = pd.Series(False, index=daily.index); bh_entry.iloc[0] = True
    bh_exit = pd.Series(False, index=daily.index)
    out.append(run_variant("B&H NIFTYBEES (daily, full)", daily, bh_entry, bh_exit,
                           FEE_LOW, stop=-1.0, tax_on=False))
    out.append(run_variant("B&H NIFTYBEES AFTER-TAX", daily, bh_entry, bh_exit,
                           FEE_LOW, stop=-1.0, tax_on=True))

    # V3-nostop (the migrated config) — pre-tax must match README (170.03 / 5.82 / 2.63)
    d = daily.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    de = ((d["Close"] > d["sma200"]) & (d["Close"].shift(1) <= d["sma200"].shift(1))).fillna(False)
    dx = ((d["Close"] < d["sma200"]) & (d["Close"].shift(1) >= d["sma200"].shift(1))).fillna(False)
    out.append(run_variant("V3-nostop (pre-tax, cross-check)", daily, de, dx, FEE_LOW,
                           stop=-1.0, tax_on=False))
    out.append(run_variant("V3-nostop AFTER-TAX", daily, de, dx, FEE_LOW,
                           stop=-1.0, tax_on=True))

    # V3 with stop (for contrast)
    out.append(run_variant("V3 +5% stop AFTER-TAX", daily, de, dx, FEE_LOW,
                           stop=STOP, tax_on=True))

    # V1 current (hourly) after-tax for completeness
    he, hx = rb.sma_cross_signals(hourly, 10, 30)
    out.append(run_variant("V1 CURRENT hourly 10/30 AFTER-TAX", hourly, he, hx, FEE_LOW,
                           gate=gate, max_hold_bars=1, tax_on=True))

    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "sharpe"]
    tbl = pd.DataFrame(out)[cols]
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 40)
    print(tbl.to_string(index=False))
    print(f"\n(full-history window = {years_full:.1f} years; India tax: 30% on gains, "
          f"1% TDS/sell, no loss offset)")

    # explicit cross-check assertion
    pre = next(m for m in out if m["variant"] == "V3-nostop (pre-tax, cross-check)")
    assert abs(pre["total_return_pct"] - 170.03) < 0.5, \
        f"CROSS-CHECK FAIL: pre-tax V3-nostop = {pre['total_return_pct']}%, expected ~170.03%"
    print("CROSS-CHECK OK: pre-tax V3-nostop reproduces run_backtest.py (170.03%).")

    with open(os.path.join(HERE, "tax_analysis_results.json"), "w") as f:
        json.dump({"tax_rate": TAX_RATE, "tds": TDS, "years": round(years_full, 1),
                   "results": out}, f, indent=2)
    print("Wrote nifty_backtest/tax_analysis_results.json")


if __name__ == "__main__":
    main()
