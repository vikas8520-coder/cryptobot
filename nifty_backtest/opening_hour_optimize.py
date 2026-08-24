#!/usr/bin/env python3
"""opening_hour_optimize.py — Find the best target/stop for the 9:15-10:00 opening hour.

The hourly analysis showed the opening hour has the best win rate (55.8%) but
still loses with 4% target / 5% stop. This script sweeps target/stop combos
to find what actually works for the opening hour specifically.
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
    LOT, DELTA, EMA_PERIOD, CSV5, CACHE
)

SPREAD = 1.0

def run_opening_sweep():
    df = load_csv(CSV5)
    n = len(df)
    ho, hc, hh, hl = ha_candles(df)
    ema_high = ema(df["High"], EMA_PERIOD).values
    ema_low = ema(df["Low"], EMA_PERIOD).values

    frames = load_bhavcopies(CACHE)
    if not frames:
        print("No BhavCopy data")
        return

    df["day"] = df["Date"].dt.normalize()
    bias_map = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        positions = np.where(mask.values)[0]
        if len(positions) < 2:
            continue
        ha_open_day = ho[positions[0]]
        ha_close_day = hc[positions[-1]]
        bias_map[day] = "call" if ha_close_day > ha_open_day else "put"

    # Collect ALL opening-hour signals (9:15-10:00) with their index-level data
    signals = []
    for i in range(EMA_PERIOD + 1, n - 1):
        t = pd.Timestamp(df["Date"].iloc[i]).time()
        if t < time(9, 15) or t > time(9, 59):
            continue

        entry_day = pd.Timestamp(df["Date"].iloc[i]).normalize()
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

        prev_day = None
        for d in sorted(bias_map.keys()):
            if d < entry_day:
                prev_day = d
            else:
                break
        if prev_day and bias_map.get(prev_day) != side:
            continue

        frame_days = sorted(frames.keys())
        opt_day = max([d for d in frame_days if d <= entry_day], default=frame_days[0] if frame_days else None)
        if opt_day is None:
            continue
        opt = select_option({opt_day: frames[opt_day]}, opt_day, opt_type)
        if opt is None:
            continue

        entry_premium = opt["premium"] + SPREAD
        if entry_premium <= 0:
            continue

        # Collect forward index moves for this signal
        forward_moves = []
        for j in range(i + 1, min(i + 75, n)):
            bar_day = pd.Timestamp(df["Date"].iloc[j]).normalize()
            if bar_day != entry_day:
                break
            index_move = df["Close"].values[j] - df["Close"].values[i]
            forward_moves.append(index_move)

        signals.append({
            "time": str(t),
            "side": side,
            "entry": entry_premium,
            "delta_sign": 1 if side == "call" else -1,
            "forward_moves": forward_moves,
        })

    print(f"\n{'='*80}")
    print(f"OPENING HOUR (9:15-10:00) TARGET/STOP OPTIMIZATION")
    print(f"Signals found: {len(signals)}")
    print(f"{'='*80}\n")

    # Sweep target/stop combinations
    targets = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]
    stops = [0.02, 0.03, 0.04, 0.05, 0.06]

    print(f"{'Target':>8} {'Stop':>8} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Avg P&L%':>10} "
          f"{'Total ₹':>10} {'PF':>7} {'Max DD ₹':>10}")
    print("-" * 85)

    best_pf = 0
    best_combo = None
    best_total = -999999
    best_combo_total = None

    for target_pct in targets:
        for stop_pct in stops:
            results = []
            for sig in signals:
                entry = sig["entry"]
                target_price = entry * (1 + target_pct)
                stop_price = entry * (1 - stop_pct)
                delta_sign = sig["delta_sign"]

                result = None
                exit_premium = None

                for idx_move in sig["forward_moves"]:
                    opt_move_pct = (idx_move * DELTA / entry) * 100
                    est_premium = entry * (1 + delta_sign * opt_move_pct / 100)

                    if est_premium >= target_price:
                        result = "target"
                        exit_premium = target_price - SPREAD
                        break
                    if est_premium <= stop_price:
                        result = "stop"
                        exit_premium = stop_price - SPREAD
                        break

                if result is None:
                    exit_premium = entry - SPREAD * 2
                    result = "eod"

                pnl_pct = (exit_premium - entry) / entry * 100
                pnl_rs = (exit_premium - entry) * LOT
                results.append({"pnl_pct": pnl_pct, "pnl_rs": pnl_rs, "result": result})

            if not results:
                continue

            wins = [r for r in results if r["pnl_pct"] > 0]
            losses = [r for r in results if r["pnl_pct"] <= 0]
            win_rate = len(wins) / len(results) * 100
            avg_pnl = sum(r["pnl_pct"] for r in results) / len(results)
            total_rs = sum(r["pnl_rs"] for r in results)
            gross_w = sum(r["pnl_pct"] for r in wins)
            gross_l = abs(sum(r["pnl_pct"] for r in losses))
            pf = gross_w / gross_l if gross_l > 0 else float('inf')

            # Max drawdown
            equity = 0
            peak = 0
            max_dd = 0
            for r in results:
                equity += r["pnl_rs"]
                if equity > peak:
                    peak = equity
                dd = peak - equity
                if dd > max_dd:
                    max_dd = dd

            print(f"{target_pct*100:>7.0f}% {stop_pct*100:>7.0f}% {len(results):>7} {len(wins):>6} "
                  f"{win_rate:>6.1f}% {avg_pnl:>+9.2f}% {total_rs:>+10.0f} {pf:>7.2f} {max_dd:>10.0f}")

            if pf > best_pf and len(results) >= 20:
                best_pf = pf
                best_combo = (target_pct, stop_pct, win_rate, total_rs, pf, len(results))
            if total_rs > best_total and len(results) >= 20:
                best_total = total_rs
                best_combo_total = (target_pct, stop_pct, win_rate, total_rs, pf, len(results))

    print(f"\n{'='*80}")
    if best_combo:
        print(f"BEST PROFIT FACTOR: {best_combo[0]*100:.0f}% target / {best_combo[1]*100:.0f}% stop")
        print(f"  {best_combo[5]} trades, {best_combo[2]:.1f}% win, ₹{best_combo[3]:+.0f} total, PF {best_combo[4]:.2f}")
    if best_combo_total:
        print(f"BEST TOTAL P&L: {best_combo_total[0]*100:.0f}% target / {best_combo_total[1]*100:.0f}% stop")
        print(f"  {best_combo_total[5]} trades, {best_combo_total[2]:.1f}% win, ₹{best_combo_total[3]:+.0f} total, PF {best_combo_total[4]:.2f}")

    # Also test: what if we ONLY trade the first 15 minutes (9:15-9:30)?
    print(f"\n{'='*80}")
    print("FIRST 15 MINUTES ONLY (9:15-9:30) — peak volatility")
    print(f"{'='*80}\n")

    early_signals = [s for s in signals if s["time"] <= "09:30:00"]
    print(f"Signals in 9:15-9:30: {len(early_signals)}")

    if early_signals:
        for target_pct, stop_pct in [(0.03, 0.03), (0.04, 0.03), (0.05, 0.03), (0.04, 0.04), (0.05, 0.05)]:
            results = []
            for sig in early_signals:
                entry = sig["entry"]
                target_price = entry * (1 + target_pct)
                stop_price = entry * (1 - stop_pct)
                delta_sign = sig["delta_sign"]

                result = None
                exit_premium = None
                for idx_move in sig["forward_moves"]:
                    opt_move_pct = (idx_move * DELTA / entry) * 100
                    est_premium = entry * (1 + delta_sign * opt_move_pct / 100)
                    if est_premium >= target_price:
                        result = "target"
                        exit_premium = target_price - SPREAD
                        break
                    if est_premium <= stop_price:
                        result = "stop"
                        exit_premium = stop_price - SPREAD
                        break
                if result is None:
                    exit_premium = entry - SPREAD * 2
                    result = "eod"
                pnl_pct = (exit_premium - entry) / entry * 100
                pnl_rs = (exit_premium - entry) * LOT
                results.append({"pnl_pct": pnl_pct, "pnl_rs": pnl_rs})

            wins = [r for r in results if r["pnl_pct"] > 0]
            losses = [r for r in results if r["pnl_pct"] <= 0]
            wr = len(wins) / len(results) * 100 if results else 0
            avg = sum(r["pnl_pct"] for r in results) / len(results) if results else 0
            total = sum(r["pnl_rs"] for r in results)
            gw = sum(r["pnl_pct"] for r in wins)
            gl = abs(sum(r["pnl_pct"] for r in losses))
            pf = gw / gl if gl > 0 else float('inf')
            print(f"  {target_pct*100:.0f}%/{stop_pct*100:.0f}%: {len(results)} trades, {wr:.1f}% win, "
                  f"avg {avg:+.2f}%, ₹{total:+.0f}, PF {pf:.2f}")


if __name__ == "__main__":
    run_opening_sweep()
