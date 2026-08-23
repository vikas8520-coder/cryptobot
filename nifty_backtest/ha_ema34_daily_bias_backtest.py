#!/usr/bin/env python3
"""ha_ema34_daily_bias_backtest.py -- offline backtest of the locked Heikin-Ashi +
EMA34 daily-bias strategy (buy CE on green bias, PE on red bias), daily-TF
approximation.

WHY this exists: the locked spec is intraday (5-minute execution TF, entry on 5m
close crossing EMA34 of highs/lows, SL = previous 5m candle), but 5m history is
capped at ~60 days (yfinance) and no longer minute archive is available offline.
This file validates the daily-scale logic -- HA bias direction, EMA34(High) /
EMA34(Low) positioning, candle-by-candle trailing -- on 26y of daily NIFTY data
so the signal logic is tested before any intraday buildout. It is an
APPROXIMATION, not the 5m strategy.

Execution model (matches run_backtest.py conventions): signals computed on bar
close, filled at the NEXT bar's close (no look-ahead). Stop checked intrabar,
filled at the stop price. Long AND short in index points (the CE/PE side is the
bias direction). Fees per side in bps (0% and 5bps variants).

Known approximations (reported honestly, not hidden):
- 5m entry/SL granularity -> daily bars. Prev-5m-candle SL is a few points; a
  prev-DAY low/high SL is hundreds of points, so stop-outs are much rarer here.
- EMA34(5m highs/lows) -> EMA34(daily High) / EMA34(daily Low).
- Options premium P&L (delta/gamma/theta/vega) is NOT modeled -- equity is the
  raw index move, long or short, leverage-free.
- 2000-2007 rows come from GOOGLEFINANCE via GitHub (Volume=0; not used here).
"""

import json
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "..", "data", "NIFTY50_index_daily_2000_2026.csv")
OUT = os.path.join(HERE, "ha_ema34_daily_bias_results.json")

EMA_SPAN = 34
FEE_LOW = 0.0005  # 5 bps per side (harness default)
FEE_ZERO = 0.0


def load_csv(path):
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.sort_values("Date").reset_index(drop=True)
    return df


def ha_candles(df):
    """Heikin Ashi from regular OHLC (standard recursion: ha_open carries the
    prior ha_open/ha_close average forward)."""
    o = df["Open"].values
    h = df["High"].values
    l = df["Low"].values
    c = df["Close"].values
    n = len(df)
    ho = np.empty(n)
    hc = np.empty(n)
    hh = np.empty(n)
    hl = np.empty(n)
    ho[0] = o[0]
    for i in range(n):
        hc[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        if i > 0:
            ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
        hh[i] = max(h[i], ho[i], hc[i])
        hl[i] = min(l[i], ho[i], hc[i])
    return ho, hc, hh, hl


def ema(s, span):
    return pd.Series(s).ewm(span=span, adjust=False).mean()


def signals(df):
    """Return (entry, exit) bool series per the daily approximation:
    ent[j] = bias[j-1] green & close[j] > ema_high[j]  (long)
             bias[j-1] red   & close[j] < ema_low[j]   (short)
    exit = bias flip, only used by the bias-flip variant."""
    ho, hc, _, _ = ha_candles(df)
    bias = pd.Series(hc > ho, index=df.index)  # green HA candle = bullish
    ema_high = ema(df["High"], EMA_SPAN)
    ema_low = ema(df["Low"], EMA_SPAN)
    warm = np.arange(len(df)) >= EMA_SPAN  # let the EMAs settle

    long_ent = (bias.shift(1).fillna(False) & (df["Close"] > ema_high)).fillna(False)
    short_ent = ((~bias.shift(1).fillna(False)) & (df["Close"] < ema_low)).fillna(False)
    # bias-flip exits use the same prev-bar-bias convention: exit signal at bar j
    # close when the bias at j (from HA candle j-1) flips vs the trade direction
    long_exit = (~bias.shift(1).fillna(False)).values
    short_exit = (bias.shift(1).fillna(False)).values
    for s in (long_ent, short_ent):
        s[~warm] = False
    return long_ent, short_ent, long_exit, short_exit


def backtest(df, long_ent, short_ent, long_exit, short_exit, fee_side,
             exit_on_bias_flip):
    """Long/short bar-driven backtester. Entry signal at bar j -> fill at close of
    bar j+1, initial SL = low/high of bar j (the 'previous candle'). Each bar after
    entry, SL trails to the previous bar's low (long) / high (short). Stop checked
    intrabar (conservative: fill at stop px)."""
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    dates = df["Date"]
    n = len(df)

    le = long_ent.values
    se = short_ent.values
    lx = long_exit
    sx = short_exit

    in_pos = 0  # +1 long, -1 short
    entry_px = 0.0
    entry_i = 0
    sl = 0.0
    equity = np.ones(n)
    eq = 1.0
    trades = []

    for i in range(1, n):
        px = close[i]
        if in_pos != 0:
            # trail: SL follows the previous candle before this bar's action
            if in_pos == 1:
                sl = max(sl, low[i - 1])
            else:
                sl = min(sl, high[i - 1])
            fill = px
            reason = None
            stop_hit = (in_pos == 1 and low[i] <= sl) or (in_pos == -1 and high[i] >= sl)
            if stop_hit:
                fill = sl
                reason = "stop"
            elif exit_on_bias_flip and (
                    (in_pos == 1 and lx[i - 1]) or (in_pos == -1 and sx[i - 1])):
                fill = px
                reason = "bias_flip"
            if reason:
                gross = fill / entry_px if in_pos == 1 else 2.0 - fill / entry_px
                net = gross * (1 - fee_side) ** 2
                eq *= net
                trades.append({"entry": str(dates[entry_i]), "exit": str(dates[i]),
                               "bars": i - entry_i, "ret": net - 1, "reason": reason})
                in_pos = 0
                equity[i] = eq
            else:
                equity[i] = eq * (px / entry_px if in_pos == 1 else 2.0 - px / entry_px)
        else:
            equity[i] = eq
            if le[i - 1] or se[i - 1]:
                in_pos = 1 if le[i - 1] else -1
                entry_px = px
                entry_i = i
                sl = low[i - 1] if in_pos == 1 else high[i - 1]

    if in_pos != 0:
        fill = close[-1]
        gross = fill / entry_px if in_pos == 1 else 2.0 - fill / entry_px
        net = gross * (1 - fee_side) ** 2
        eq *= net
        trades.append({"entry": str(dates[entry_i]), "exit": str(dates.iloc[-1]),
                       "bars": n - 1 - entry_i, "ret": net - 1, "reason": "eod"})
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def metrics(equity, trades, years, n_bars):
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
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
        "time_in_market_pct": round(100 * sum(t["bars"] for t in trades) / n_bars, 1),
    }


def run_variant(name, df, le, se, lx, sx, fee, exit_on_bias_flip):
    eq, trades = backtest(df, le, se, lx, sx, fee, exit_on_bias_flip)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = metrics(eq, trades, years, len(df))
    m["variant"] = name
    return m


def main():
    df = load_csv(CSV)
    le, se, lx, sx = signals(df)

    results = []

    # ---- B&H benchmark (long the index, full 26y) ----
    bh_eq = pd.Series(df["Close"].values / df["Close"].values[0], index=df["Date"])
    m = metrics(bh_eq, [], (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25,
                len(df))
    m["variant"] = "B&H NIFTY 2000-2026 (0 fee)"
    results.append(m)

    # ---- strategy variants: trail-only vs bias-flip exit, x fee ----
    for name, flip in (("pure trail", False), ("bias-flip exit", True)):
        for fee_name, fee in (("0 fee", FEE_ZERO), ("5bps/side", FEE_LOW)):
            results.append(run_variant(
                f"HA/EMA34 daily-bias {name} ({fee_name})",
                df, le, se, lx, sx, fee, flip))

    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "sharpe",
            "avg_hold_bars", "time_in_market_pct"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 220)
    print(f"Merged daily CSV: {df['Date'].iloc[0].date()} -> {df['Date'].iloc[-1].date()}"
          f" ({len(df)} bars)")
    print(tbl.to_string(index=False))

    with open(OUT, "w") as f:
        json.dump({"data": CSV,
                   "ema_span": EMA_SPAN,
                   "note": "daily-TF approximation of the locked 5m HA+EMA34 spec; "
                           "options premium P&L not modeled",
                   "results": results}, f, indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()