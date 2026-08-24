#!/usr/bin/env python3
"""orderflow_debug.py — Diagnose which filter is killing all signals."""
import os, sys
from datetime import time
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ha_prevbreak_options_backtest_filtered import load_csv, ha_candles, CSV5
from orderflow_inspired_backtest import (
    compute_volume_profile, find_swing_high_low, fib_levels,
    ha_reversal, EMA_FAST, EMA_SLOW, FIB_ENTRY_LOW, FIB_ENTRY_HIGH
)

def main():
    df = load_csv(CSV5)
    n = len(df)
    ho, hc, hh, hl = ha_candles(df)
    df["day"] = df["Date"].dt.normalize()

    # 15m regime
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    df_15m = df.set_index("Date").resample("15min").agg(agg).dropna().reset_index()
    ema20_15 = df_15m["Close"].ewm(span=EMA_FAST, adjust=False).mean().values
    ema50_15 = df_15m["Close"].ewm(span=EMA_SLOW, adjust=False).mean().values

    def get_regime(dt):
        ts = pd.Timestamp(dt).floor("15min")
        for j in range(len(df_15m)-1, -1, -1):
            if df_15m["Date"].iloc[j] <= ts and j >= EMA_SLOW:
                if np.isfinite(ema20_15[j]) and np.isfinite(ema50_15[j]):
                    if ema20_15[j] > ema50_15[j]: return "up"
                    elif ema20_15[j] < ema50_15[j]: return "down"
                    else: return "sideways"
                break
        return "sideways"

    # Previous day volume profiles
    prev_day_vp = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        day_df = df[mask]
        if len(day_df) < 10: continue
        poc, va_high, va_low = compute_volume_profile(day_df)
        if poc is not None:
            prev_day_vp[day] = (poc, va_high, va_low)

    # Previous day bias
    bias_map = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        positions = np.where(mask.values)[0]
        if len(positions) < 2: continue
        bias_map[day] = "call" if hc[positions[-1]] > ho[positions[0]] else "put"

    # Count how many bars pass each filter
    counts = {"time_window": 0, "regime_not_sideways": 0, "has_prev_vp": 0,
              "bias_matches_regime": 0, "in_fib_zone": 0, "in_value_area": 0,
              "ha_reversal_bull": 0, "ha_reversal_bear": 0, "all_filters_call": 0, "all_filters_put": 0}

    for i in range(EMA_SLOW + 5, n - 1):
        t = pd.Timestamp(df["Date"].iloc[i]).time()
        entry_day = df["day"].iloc[i]

        # 1. Time window
        if t < time(9, 15) or t > time(9, 59):
            continue
        counts["time_window"] += 1

        # 2. Regime
        regime = get_regime(df["Date"].iloc[i])
        if regime == "sideways":
            continue
        counts["regime_not_sideways"] += 1

        # 3. Has previous day VP
        prev_days = [d for d in prev_day_vp if d < entry_day]
        if not prev_days:
            continue
        prev_day = max(prev_days)
        poc, va_high, va_low = prev_day_vp[prev_day]
        if poc is None:
            continue
        counts["has_prev_vp"] += 1

        # 4. Bias matches regime
        prev_bias_days = [d for d in bias_map if d < entry_day]
        if not prev_bias_days:
            continue
        bias = bias_map[max(prev_bias_days)]
        if (regime == "up" and bias != "call") or (regime == "down" and bias != "put"):
            continue
        counts["bias_matches_regime"] += 1

        # 5. In Fib zone
        prev_day_mask = df["day"] == prev_day
        prev_day_df = df[prev_day_mask]
        swing_high, swing_low = find_swing_high_low(prev_day_df, lookback=len(prev_day_df))
        if swing_high <= swing_low:
            continue
        fibs = fib_levels(swing_low, swing_high)
        close_i = df["Close"].values[i]

        if regime == "up":
            fib_low = fibs.get(FIB_ENTRY_LOW)
            fib_high = fibs.get(FIB_ENTRY_HIGH)
            if fib_low and fib_high and fib_low <= close_i <= fib_high:
                counts["in_fib_zone"] += 1
                # 6. In value area (discount)
                if close_i <= va_high:
                    counts["in_value_area"] += 1
                    # 7. HA reversal
                    if ha_reversal(ho, hc, i, "bullish"):
                        counts["ha_reversal_bull"] += 1
                        counts["all_filters_call"] += 1

        elif regime == "down":
            fib_low = fibs.get(1 - FIB_ENTRY_HIGH)
            fib_high = fibs.get(1 - FIB_ENTRY_LOW)
            if fib_low and fib_high and fib_low <= close_i <= fib_high:
                counts["in_fib_zone"] += 1
                if close_i >= va_low:
                    counts["in_value_area"] += 1
                    if ha_reversal(ho, hc, i, "bearish"):
                        counts["ha_reversal_bear"] += 1
                        counts["all_filters_put"] += 1

    print("FILTER FUNNEL (how many 5m bars in 9:15-10:00 pass each stage):")
    print(f"  1. Time window (9:15-10:00):          {counts['time_window']:>5}")
    print(f"  2. Regime not sideways:               {counts['regime_not_sideways']:>5}")
    print(f"  3. Has previous day VP:               {counts['has_prev_vp']:>5}")
    print(f"  4. Bias matches regime:               {counts['bias_matches_regime']:>5}")
    print(f"  5. Price in Fib 0.705-0.886 zone:     {counts['in_fib_zone']:>5}")
    print(f"  6. Price in value area (discount):    {counts['in_value_area']:>5}")
    print(f"  7. HA bullish reversal:               {counts['ha_reversal_bull']:>5}")
    print(f"  7. HA bearish reversal:               {counts['ha_reversal_bear']:>5}")
    print(f"  ALL FILTERS (call):                   {counts['all_filters_call']:>5}")
    print(f"  ALL FILTERS (put):                    {counts['all_filters_put']:>5}")

    # Show what the Fib zones look like for a few days
    print("\nSample Fib zones (first 5 days with data):")
    shown = 0
    for day in sorted(df["day"].unique()):
        if shown >= 5: break
        mask = df["day"] == day
        day_df = df[mask]
        if len(day_df) < 10: continue
        sh, sl = find_swing_high_low(day_df, lookback=len(day_df))
        fibs = fib_levels(sl, sh)
        poc, vah, val = compute_volume_profile(day_df)
        print(f"  {day.date()}: swing {sl:.0f}-{sh:.0f} | Fib 0.705={fibs.get(0.705,0):.0f} 0.886={fibs.get(0.886,0):.0f} | POC={poc:.0f} VA={val:.0f}-{vah:.0f}")
        shown += 1

if __name__ == "__main__":
    main()
