#!/usr/bin/env python3
"""ha_ema34_5m_backtest.py -- the locked HA + EMA34 daily-bias strategy executed
at its TRUE 5-minute timeframe, over the ~60 days of 5m history yfinance
provides (^NSEI). Daily bias comes from the PREVIOUS day's HA candle (fresh
daily bars over the same window), EMA34 is on 5m High / 5m Low, entry is a 5m
close crossing the EMA, initial SL is the previous 5m candle's low/high, then
trailed candle by candle.

Execution model (matches run_backtest.py / the daily script): signals at bar
close, filled at the NEXT bar's close; stops checked intrabar, filled at the
stop price; long AND short in raw index points (side = bias direction); fees
per side (0% and 5bps).

Why this file exists: the daily-TF approximation
(ha_ema34_daily_bias_backtest) validated the signal logic on 26y of daily bars
but with prev-DAY stops (hundreds of pts). This run measures the strategy at
realistic 5m granularity -- tight stops, far more stop-outs -- over the only
5m history available offline (~57 trading days), and re-runs the daily model on
the SAME window for an apples-to-apples comparison.

Remaining honest caveats:
- Options premium P&L (delta/gamma/theta/vega) is NOT modeled -- equity is the
  raw index move, long or short, leverage-free.
- ~57 trading days is a small sample; the daily comparison run is even smaller
  (the 34-bar EMA warm-up eats 34 of the 57 daily bars).
"""

import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ha_ema34_daily_bias_backtest as dbt  # reuse proven logic (noqa: E402)

CSV5 = os.path.join(HERE, "..", "data", "NSEI_index_5m.csv")
CSVD = os.path.join(HERE, "..", "data", "NSEI_index_daily_recent.csv")
OUT = os.path.join(HERE, "ha_ema34_5m_results.json")

EMA_SPAN = 34
FEE_LOW = 0.0005  # 5 bps per side (harness default)
FEE_ZERO = 0.0


def load_csv(path):
    """Our CSVs are index-first (datetime) -- normalize to a Date column."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = df.sort_index().reset_index()
    df = df.rename(columns={df.columns[0]: "Date"})
    return df


def signals_5m(df5, dfd):
    """5m entries/exits. bias for a 5m bar on day D = HA bias of day D-1
    (the PREVIOUS day, per the locked spec); EMA34 computed on 5m High/Low."""
    ho, hc, _, _ = dbt.ha_candles(dfd)
    bias_daily = pd.Series(hc > ho, index=dfd["Date"])
    # audit 2026-08-17: reindexing against raw 5m timestamps matched nothing
    # (daily index is midnight, 5m is 09:15:00+) -> bias was all-False. Normalize
    # the 5m timestamps to dates so the day-D -> bias(D-1) map actually applies.
    bias = (bias_daily.shift(1)
            .reindex(df5["Date"].dt.normalize())
            .fillna(False)
            .reset_index(drop=True))
    ema_high = dbt.ema(df5["High"], EMA_SPAN)
    ema_low = dbt.ema(df5["Low"], EMA_SPAN)
    warm = np.arange(len(df5)) >= EMA_SPAN

    long_ent = (bias & (df5["Close"] > ema_high)).fillna(False)
    short_ent = ((~bias) & (df5["Close"] < ema_low)).fillna(False)
    # bias-flip exits: same prev-day-bias convention, one-bar-late evaluation
    long_exit = (~bias).values
    short_exit = bias.values
    for s in (long_ent, short_ent):
        s[~warm] = False
    return long_ent, short_ent, long_exit, short_exit


def run_variant(name, df, le, se, lx, sx, fee, exit_on_bias_flip):
    eq, trades = dbt.backtest(df, le, se, lx, sx, fee, exit_on_bias_flip)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = dbt.metrics(eq, trades, years, len(df))
    m["variant"] = name
    return m, eq


def benchmark(name, df):
    bh_eq = pd.Series(df["Close"].values / df["Close"].values[0], index=df["Date"])
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = dbt.metrics(bh_eq, [], years, len(df))
    m["variant"] = name
    return m


def main():
    df5 = load_csv(CSV5)
    dfd = load_csv(CSVD)

    results = []

    # ---- B&H benchmarks on the shared ~57-day window ----
    results.append(benchmark(f"B&H ^NSEI 5m window (0 fee)", df5))
    results.append(benchmark(f"B&H ^NSEI daily window (0 fee)", dfd))

    # ---- 5m strategy (true execution TF) ----
    le5, se5, lx5, sx5 = signals_5m(df5, dfd)
    for name, flip in (("pure trail", False), ("bias-flip exit", True)):
        for fee_name, fee in (("0 fee", FEE_ZERO), ("5bps/side", FEE_LOW)):
            m, _ = run_variant(f"5m {name} ({fee_name})", df5, le5, se5, lx5, sx5,
                               fee, flip)
            results.append(m)

    # ---- daily approximation on the SAME window (for comparison) ----
    led, sed, lxd, sxd = dbt.signals(dfd)
    for name, flip in (("pure trail", False), ("bias-flip exit", True)):
        for fee_name, fee in (("0 fee", FEE_ZERO), ("5bps/side", FEE_LOW)):
            m, _ = run_variant(f"daily-window {name} ({fee_name})", dfd, led, sed,
                               lxd, sxd, fee, flip)
            results.append(m)

    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "sharpe",
            "avg_hold_bars", "time_in_market_pct"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 240)
    print(f"5m data: {df5['Date'].iloc[0].date()} -> {df5['Date'].iloc[-1].date()}"
          f" ({len(df5)} bars)")
    print(f"daily data: {dfd['Date'].iloc[0].date()} -> {dfd['Date'].iloc[-1].date()}"
          f" ({len(dfd)} bars)")
    print(tbl.to_string(index=False))

    with open(OUT, "w") as f:
        json.dump({"data_5m": CSV5, "data_daily": CSVD, "ema_span": EMA_SPAN,
                   "note": "true 5m-TF backtest of the locked HA+EMA34 spec over "
                           "~57 trading days; options premium P&L not modeled; "
                           "daily-window rows are the daily approximation on the "
                           "same dates for comparison",
                   "results": results}, f, indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()