#!/usr/bin/env python3
"""hourly_edge_analysis.py — Break down the Nifty 5m HA breakout strategy by entry hour.

Tests the user's hypothesis that the 9:00-10:00 opening hour (high volatility)
produces better signals than the rest of the day.

For each hour bucket (9:15-10:00, 10:00-11:00, ... 14:00-15:00):
  - Number of entry signals
  - Win rate
  - Average return per trade (before and after spread)
  - Total P&L if you ONLY traded that hour
  - Profit factor

This is an honest analysis — if the opening hour is noise (false breakouts),
the data will show it. If it's edge, the data will show that too.
"""
import os
import sys
from datetime import time
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ha_prevbreak_options_backtest_filtered import (
    load_csv, ha_candles, ema, load_bhavcopies, select_option,
    LOT, DELTA, EMA_PERIOD, TARGET_PCT, STOP_PCT, CSV5, CACHE
)

SPREAD = 1.0  # per-side spread in ₹

def run_hourly(target_pct=0.04, stop_pct=0.05, spread=SPREAD):
    df = load_csv(CSV5)
    n = len(df)
    if n < EMA_PERIOD + 2:
        print("Not enough data")
        return

    ho, hc, hh, hl = ha_candles(df)
    ema_high = ema(df["High"], EMA_PERIOD).values
    ema_low = ema(df["Low"], EMA_PERIOD).values

    frames = load_bhavcopies(CACHE)
    if not frames:
        print("No BhavCopy data found in", CACHE)
        return

    # Build daily bias map (previous day HA close vs open)
    df["day"] = df["Date"].dt.normalize()
    bias_map = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        positions = np.where(mask.values)[0]
        if len(positions) < 2:
            continue
        pos_start = positions[0]
        pos_end = positions[-1]
        ha_open_day = ho[pos_start]
        ha_close_day = hc[pos_end]
        bias_map[day] = "call" if ha_close_day > ha_open_day else "put"

    # Hour buckets: 9:15-10:00, 10:00-11:00, 11:00-12:00, 12:00-13:00, 13:00-14:00, 14:00-15:00
    hour_buckets = {
        "9:15-10:00": (time(9, 15), time(9, 59)),
        "10:00-11:00": (time(10, 0), time(10, 59)),
        "11:00-12:00": (time(11, 0), time(11, 59)),
        "12:00-13:00": (time(12, 0), time(12, 59)),
        "13:00-14:00": (time(13, 0), time(13, 59)),
        "14:00-15:00": (time(14, 0), time(14, 59)),
    }

    # Collect trades per hour bucket
    bucket_trades = defaultdict(list)

    for i in range(EMA_PERIOD + 1, n - 1):
        t = pd.Timestamp(df["Date"].iloc[i]).time()
        entry_day = pd.Timestamp(df["Date"].iloc[i]).normalize()

        # Determine which hour bucket this bar falls in
        bucket_name = None
        for bname, (bstart, bend) in hour_buckets.items():
            if bstart <= t <= bend:
                bucket_name = bname
                break
        if bucket_name is None:
            continue

        # HA breakout check
        call_break = df["High"].values[i] - hh[i - 1]
        put_break = hl[i - 1] - df["Low"].values[i]
        call = call_break > 0
        put = put_break > 0
        close_i = df["Close"].values[i]
        ema_ok = np.isfinite(ema_high[i]) and np.isfinite(ema_low[i])

        if call:
            call = (ema_ok and close_i > ema_high[i] and close_i > ema_low[i] and close_i > hh[i - 1])
        if put:
            put = (ema_ok and close_i < ema_high[i] and close_i < ema_low[i] and close_i < hl[i - 1])

        if call and put:
            if call_break >= put_break:
                put = False
            else:
                call = False
        if not call and not put:
            continue

        side = "call" if call else "put"
        opt_type = "CE" if call else "PE"

        # Daily bias filter
        prev_day = None
        for d in sorted(bias_map.keys()):
            if d < entry_day:
                prev_day = d
            else:
                break
        if prev_day and bias_map.get(prev_day) != side:
            continue

        # Option selection
        frame_days = sorted(frames.keys())
        opt_day = max([d for d in frame_days if d <= entry_day], default=frame_days[0] if frame_days else None)
        if opt_day is None:
            continue
        opt = select_option({opt_day: frames[opt_day]}, opt_day, opt_type)
        if opt is None:
            continue

        entry_premium = opt["premium"] + spread
        if entry_premium <= 0:
            continue

        # Simulate outcome: scan forward for target or stop
        target_price = entry_premium * (1 + target_pct)
        stop_price = entry_premium * (1 - stop_pct)

        result = None
        exit_premium = None
        exit_reason = None
        exit_bar = None

        for j in range(i + 1, min(i + 75, n)):  # max 75 bars (~6 hours)
            bar_day = pd.Timestamp(df["Date"].iloc[j]).normalize()
            if bar_day != entry_day:
                # EOD exit: use option close for the next available day
                break

            # Estimate option price using delta
            index_move = df["Close"].values[j] - df["Close"].values[i]
            opt_move = index_move * DELTA / entry_premium * 100  # rough % move
            est_premium = entry_premium * (1 + opt_move / 100)

            if est_premium >= target_price:
                result = "target"
                exit_premium = target_price - spread
                exit_bar = j
                break
            if est_premium <= stop_price:
                result = "stop"
                exit_premium = stop_price - spread
                exit_bar = j
                break

        if result is None:
            # EOD exit — assume flat
            exit_premium = entry_premium - spread * 2
            result = "eod"

        pnl_pct = (exit_premium - entry_premium) / entry_premium * 100
        pnl_rs = (exit_premium - entry_premium) * LOT

        bucket_trades[bucket_name].append({
            "time": str(t),
            "side": side,
            "entry": entry_premium,
            "exit": exit_premium,
            "pnl_pct": pnl_pct,
            "pnl_rs": pnl_rs,
            "result": result,
            "bars_held": (exit_bar - i) if exit_bar else 75,
        })

    # Print results
    print(f"\n{'='*90}")
    print(f"NIFTY 5M HOURLY EDGE ANALYSIS")
    print(f"Period: {df['Date'].iloc[0]} to {df['Date'].iloc[-1]}")
    print(f"Target: +{target_pct*100:.0f}%  Stop: -{stop_pct*100:.0f}%  Spread: ₹{spread}/side  Lot: {LOT}")
    print(f"{'='*90}\n")

    print(f"{'Hour':<14} {'Trades':>7} {'Wins':>6} {'Loss':>6} {'Win%':>7} {'Avg P&L%':>10} "
          f"{'Total ₹':>10} {'PF':>7} {'Avg Win%':>10} {'Avg Loss%':>10}")
    print("-" * 100)

    all_trades = []
    for bname in ["9:15-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00", "13:00-14:00", "14:00-15:00"]:
        trades = bucket_trades.get(bname, [])
        if not trades:
            print(f"{bname:<14} {'0':>7}")
            continue
        all_trades.extend(trades)
        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        win_rate = len(wins) / len(trades) * 100
        avg_pnl = sum(t["pnl_pct"] for t in trades) / len(trades)
        total_rs = sum(t["pnl_rs"] for t in trades)
        gross_w = sum(t["pnl_pct"] for t in wins)
        gross_l = abs(sum(t["pnl_pct"] for t in losses))
        pf = gross_w / gross_l if gross_l > 0 else float('inf')
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

        print(f"{bname:<14} {len(trades):>7} {len(wins):>6} {len(losses):>6} {win_rate:>6.1f}% "
              f"{avg_pnl:>+9.2f}% {total_rs:>+10.0f} {pf:>7.2f} {avg_win:>+9.2f}% {avg_loss:>+9.2f}%")

    # Totals
    if all_trades:
        wins = [t for t in all_trades if t["pnl_pct"] > 0]
        losses = [t for t in all_trades if t["pnl_pct"] <= 0]
        win_rate = len(wins) / len(all_trades) * 100
        avg_pnl = sum(t["pnl_pct"] for t in all_trades) / len(all_trades)
        total_rs = sum(t["pnl_rs"] for t in all_trades)
        gross_w = sum(t["pnl_pct"] for t in wins)
        gross_l = abs(sum(t["pnl_pct"] for t in losses))
        pf = gross_w / gross_l if gross_l > 0 else float('inf')
        avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        print("-" * 100)
        print(f"{'ALL HOURS':<14} {len(all_trades):>7} {len(wins):>6} {len(losses):>6} {win_rate:>6.1f}% "
              f"{avg_pnl:>+9.2f}% {total_rs:>+10.0f} {pf:>7.2f} {avg_win:>+9.2f}% {avg_loss:>+9.2f}%")

    # Now test: what if we ONLY trade 9:15-10:00 vs ONLY 10:00-14:30 (current)?
    print(f"\n{'='*90}")
    print("COMPARISON: Opening hour vs Current window")
    print(f"{'='*90}\n")

    opening = bucket_trades.get("9:15-10:00", [])
    current = []
    for bname in ["10:00-11:00", "11:00-12:00", "12:00-13:00", "13:00-14:00", "14:00-15:00"]:
        current.extend(bucket_trades.get(bname, []))

    for label, trades in [("9:15-10:00 (opening)", opening), ("10:00-15:00 (current)", current)]:
        if not trades:
            print(f"{label}: no trades")
            continue
        wins = [t for t in trades if t["pnl_pct"] > 0]
        losses = [t for t in trades if t["pnl_pct"] <= 0]
        wr = len(wins) / len(trades) * 100
        avg = sum(t["pnl_pct"] for t in trades) / len(trades)
        total = sum(t["pnl_rs"] for t in trades)
        gw = sum(t["pnl_pct"] for t in wins)
        gl = abs(sum(t["pnl_pct"] for t in losses))
        pf = gw / gl if gl > 0 else float('inf')
        print(f"{label}: {len(trades)} trades, {wr:.1f}% win, avg {avg:+.2f}%, total ₹{total:+.0f}, PF {pf:.2f}")

    # Per-trade detail for opening hour
    if opening:
        print(f"\n{'='*90}")
        print("OPENING HOUR (9:15-10:00) — ALL TRADES")
        print(f"{'='*90}\n")
        print(f"{'Date':<12} {'Time':<8} {'Side':<5} {'Entry':>8} {'Exit':>8} {'P&L%':>8} {'₹':>8} {'Result':<8} {'Bars':>5}")
        for t in opening:
            print(f"{t['time'][:10] if len(t['time'])>10 else '':<12} {t['time']:<8} {t['side']:<5} "
                  f"₹{t['entry']:>6.0f} ₹{t['exit']:>6.0f} {t['pnl_pct']:>+7.2f}% ₹{t['pnl_rs']:>+7.0f} "
                  f"{t['result']:<8} {t['bars_held']:>5}")


if __name__ == "__main__":
    run_hourly()
