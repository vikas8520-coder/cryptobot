#!/usr/bin/env python3
"""Offline backtest harness for the NIFTYBEES paper bot (freeze-safe, standalone).

WHY this exists: freeze period 2026-07 — strategy changes are paper-only, so we
measure the live config (config_nifty.json spx_strategy block) against sane
alternatives WITHOUT touching nifty_engine.py / spx_strategy.py / any config.
Reads pre-exported CSVs in ../data/ only. No network. No randomness.

Execution model: signals computed on bar close, filled at the NEXT bar's close
(no look-ahead). Long-only, one position, fee charged per side.

NOTE on max_hold_cycles=72: the live engine cycles every ~5s, so 72 cycles is
~6 minutes of wall clock — far less than one hourly bar. We model it as a
1-bar time-stop on hourly data (the closest honest translation) and also run
the same variant with the time-stop disabled to show its impact.
"""

import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")

FEE_LOW = 0.0005   # 5 bps per side (as configured)
FEE_HI = 0.0006    # 12 bps round trip ~= 6 bps per side (realistic India all-in)
BARS_PER_YEAR_DAILY = 252
STOP = -0.05


def load_csv(name):
    df = pd.read_csv(os.path.join(DATA, name), parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    # Data-glitch fix (found during harness validation 2026-07-25): the exported
    # daily CSV is split-ADJUSTED except two bad rows around the 2019-12-19 1:10
    # split (Close 130.2 -> 13.02 -> 13.03 -> 129.9). It's a blip, not a level
    # shift: detect a ~-90% drop that later recovers ~+900% and scale the
    # in-between rows x10. Every long-horizon stat is garbage without this.
    r = df["Close"].pct_change()
    drops = list(r[r < -0.85].index)
    pops = list(r[r > 4.0].index)
    for lo in drops:
        hi = next((p for p in pops if p > lo), None)
        if hi is not None:
            for c in ("Open", "High", "Low", "Close"):
                df.loc[lo: hi - 1, c] = df.loc[lo: hi - 1, c] * 10.0
    return df


def build_daily_sma200_gate(daily):
    """200-day SMA brake on DAILY closes -> date-indexed boolean (close > sma200)."""
    d = daily.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["gate"] = d["Close"] > d["sma200"]
    d["day"] = d["Date"].dt.date
    return d.set_index("day")[["gate", "sma200"]]


def backtest(df, entry_sig, exit_sig, fee_side, stop=STOP, max_hold_bars=0,
             gate=None):
    """Bar-driven long-only backtester. entry/exit_sig are boolean Series aligned
    to df. Signal at bar i -> execute at close of bar i+1. gate: date-indexed
    daily 200DMA brake (entry blocked when gate is False/NaN)."""
    close = df["Close"].values
    low = df["Low"].values
    dates = df["Date"]
    days = dates.dt.date.values
    n = len(df)

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
            # mark equity
            eq_now = eq * (px / entry_px)
            exit_reason = None
            fill = px
            # hard stop: check intrabar low first (conservative: fill at stop px)
            stop_px = entry_px * (1 + stop)
            if low[i] <= stop_px:
                fill = stop_px
                exit_reason = "stop"
            elif max_hold_bars and (i - entry_i) >= max_hold_bars:
                fill = px
                exit_reason = "timeout"
            elif exi[i - 1]:  # signal on prev bar close -> exit this bar close
                fill = px
                exit_reason = "signal"
            if exit_reason:
                gross = fill / entry_px
                net = gross * (1 - fee_side) ** 2
                eq *= net
                trades.append({"entry": str(dates[entry_i]), "exit": str(dates[i]),
                               "bars": i - entry_i, "ret": net - 1,
                               "reason": exit_reason})
                in_pos = False
                equity[i] = eq
            else:
                equity[i] = eq_now
        else:
            equity[i] = eq
            if ent[i - 1]:  # entry signal on prev bar close -> buy this bar close
                ok = True
                if gate is not None:
                    g = gate["gate"].get(days[i], np.nan)
                    ok = bool(g) if g == g else False
                if ok:
                    in_pos = True
                    entry_px = px
                    entry_i = i

    # close any open position at last bar
    if in_pos:
        gross = close[-1] / entry_px
        net = gross * (1 - fee_side) ** 2
        eq *= net
        trades.append({"entry": str(dates[entry_i]), "exit": str(dates.iloc[-1]),
                       "bars": n - 1 - entry_i, "ret": net - 1, "reason": "eod"})
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def metrics(equity, trades, bars_in_market, n_bars, years):
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    # daily returns of equity curve for Sharpe
    daily_eq = equity.groupby(equity.index.date).last()
    r = daily_eq.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(252)) if len(r) > 2 and r.std() > 0 else 0.0
    wins = [t for t in trades if t["ret"] > 0]
    losses = [t for t in trades if t["ret"] <= 0]
    gp = sum(t["ret"] for t in wins)
    gl = -sum(t["ret"] for t in losses)
    pf = gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0)
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(pf, 2) if np.isfinite(pf) else "inf",
        "sharpe": round(sharpe, 2),
        "avg_hold_bars": round(np.mean([t["bars"] for t in trades]), 1) if trades else 0.0,
        "time_in_market_pct": round(100 * bars_in_market / n_bars, 1),
    }


def sma_cross_signals(df, fast, slow):
    f = df["Close"].rolling(fast).mean()
    s = df["Close"].rolling(slow).mean()
    entry = (f > s) & (f.shift(1) <= s.shift(1))
    exit_ = (f < s) & (f.shift(1) >= s.shift(1))
    return entry.fillna(False), exit_.fillna(False)


def run_variant(name, df, entry, exit_, fee_side, gate=None, max_hold_bars=0,
                stop=STOP):
    eq, trades = backtest(df, entry, exit_, fee_side, stop=stop,
                          max_hold_bars=max_hold_bars, gate=gate)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    bars_in = sum(t["bars"] for t in trades)
    m = metrics(eq, trades, bars_in, len(df), years)
    m["variant"] = name
    return m


def main():
    daily = load_csv("NIFTYBEES_daily.csv")
    hourly = load_csv("NIFTYBEES_hourly.csv")
    gate = build_daily_sma200_gate(daily)

    results = []

    # ---- BUY & HOLD benchmark (daily, full history, one 5bps buy) ----
    bh_entry = pd.Series(False, index=daily.index)
    bh_entry.iloc[0] = True
    bh_exit = pd.Series(False, index=daily.index)
    results.append(run_variant("B&H NIFTYBEES (daily, full)", daily, bh_entry,
                               bh_exit, FEE_LOW, stop=-1.0))

    # B&H over hourly window for apples-to-apples with V1/V2
    d1 = daily[daily["Date"].dt.date >= hourly["Date"].iloc[0].date()].reset_index(drop=True)
    e = pd.Series(False, index=d1.index); e.iloc[0] = True
    x = pd.Series(False, index=d1.index)
    results.append(run_variant("B&H NIFTYBEES (last 1y)", d1, e, x, FEE_LOW, stop=-1.0))

    # ---- V1 CURRENT: SMA(10/30) hourly cross + 200DMA brake + 5% stop +
    #      72-cycle time-stop (~6 min => 1 hourly bar) ----
    he, hx = sma_cross_signals(hourly, 10, 30)
    results.append(run_variant("V1 CURRENT hourly 10/30 +brake +stop +1-bar timeout (5bps)",
                               hourly, he, hx, FEE_LOW, gate=gate, max_hold_bars=1))
    results.append(run_variant("V1 CURRENT (12bps RT)", hourly, he, hx, FEE_HI,
                               gate=gate, max_hold_bars=1))

    # ---- V2: same, time-stop disabled ----
    results.append(run_variant("V2 hourly 10/30 +brake +stop, NO timeout (5bps)",
                               hourly, he, hx, FEE_LOW, gate=gate, max_hold_bars=0))
    results.append(run_variant("V2 NO timeout (12bps RT)", hourly, he, hx, FEE_HI,
                               gate=gate, max_hold_bars=0))

    # ---- V3: daily 200DMA braked-hold (enter >200dma, exit <200dma, 5% stop) ----
    d = daily.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    de = ((d["Close"] > d["sma200"]) & (d["Close"].shift(1) <= d["sma200"].shift(1))).fillna(False)
    dx = ((d["Close"] < d["sma200"]) & (d["Close"].shift(1) >= d["sma200"].shift(1))).fillna(False)
    results.append(run_variant("V3 daily 200DMA braked-hold +5% stop (5bps)",
                               daily, de, dx, FEE_LOW))
    results.append(run_variant("V3 (12bps RT)", daily, de, dx, FEE_HI))
    # V3 without the hard stop: shows how much the 5% stop costs a daily system
    results.append(run_variant("V3-nostop daily 200DMA braked-hold, NO stop (5bps)",
                               daily, de, dx, FEE_LOW, stop=-1.0))

    # ---- V4: daily SMA(20/50) cross + 200DMA brake + 5% stop; and 50/200 ----
    e2050, x2050 = sma_cross_signals(daily, 20, 50)
    results.append(run_variant("V4a daily 20/50 cross +brake +stop (5bps)",
                               daily, e2050, x2050, FEE_LOW, gate=gate))
    e50200, x50200 = sma_cross_signals(daily, 50, 200)
    results.append(run_variant("V4b daily 50/200 golden cross +stop (5bps)",
                               daily, e50200, x50200, FEE_LOW))

    # ---- output ----
    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "sharpe",
            "avg_hold_bars", "time_in_market_pct"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 60)
    print(tbl.to_string(index=False))

    out = os.path.join(HERE, "results.json")
    with open(out, "w") as f:
        json.dump({"generated_from": ["NIFTYBEES_daily.csv", "NIFTYBEES_hourly.csv"],
                   "stoploss": STOP, "fee_low_side": FEE_LOW, "fee_hi_side": FEE_HI,
                   "results": results}, f, indent=2)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
