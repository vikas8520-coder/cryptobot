#!/usr/bin/env python3
"""
Walk-forward test for the SPOT ADX hypothesis (TrendFollowHopt buy_adx 20/25/30/35).

Why: a one-shot train/hold-out split showed ADX30-35 "win" on train but hold-out PF
reversed direction (ADX25=0.52 -> ADX30=0.28 -> ADX35=0.35), violating the
pre-registered "hold-out divergence -> reject" rule. Walk-forward rolls many
train->test windows so no single split can be hand-waved.

Method: rolling windows. For each window:
  - TRAIN on [start, start+2y), pick best ADX in {20,25,30,35} by train PF.
  - TEST that chosen ADX on the out-of-sample [start+2y, start+2y+6mo).
  - Record whether the train-chosen ADX beats the fixed ADX25 on the TEST slice.
Aggregate across all windows.

Freeze-safe: only calls `freqtrade backtesting` (read-only), never hyperopt, never
writes live config/strategy. Uses the existing ADX* probe subclasses.

Usage: ./.venv/bin/python3 user_data/walkforward_adx.py
"""
import subprocess
import re
import sys
from datetime import date, timedelta

CONFIG = "config.json"
STRATEGY = {
    20: "TrendFollowHoptADX20",
    25: "TrendFollowHoptADX25",
    30: "TrendFollowHoptADX30",
    35: "TrendFollowHoptADX35",
}
ADXS = [20, 25, 30, 35]
TIMEFRAME = "1h"
TRAIN_YEARS = 1
TEST_MONTHS = 3
STEP_MONTHS = 3
START = date(2019, 1, 1)   # spot data: BTC 2017, ETH 2019, SOL 2020 -> use 2019 to have all 3 pairs
END = date(2026, 1, 1)     # leave the last window's test within range


def run_backtest(strategy, timerange):
    cmd = [
        "./.venv/bin/freqtrade", "backtesting",
        "--config", CONFIG, "--strategy", strategy,
        "--timerange", timerange, "--timeframe", TIMEFRAME, "--cache", "none",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    pf = re.search(r"│ Profit factor\s+│\s+([0-9.]+)", out)
    tr = re.search(r"│ Total/Daily Avg Trades\s+│\s+(\d+)", out)
    return (
        float(pf.group(1)) if pf else None,
        int(tr.group(1)) if tr else 0,
    )


def to_tr(s, e):
    return f"{s.strftime('%Y%m%d')}-{e.strftime('%Y%m%d')}"


def main():
    windows = []
    s = START
    while True:
        t_end = s + timedelta(days=TRAIN_YEARS * 365)
        te_end = t_end + timedelta(days=TEST_MONTHS * 30)
        if te_end > END:
            break
        windows.append((s, t_end, te_end))
        s = s + timedelta(days=STEP_MONTHS * 30)  # step forward by STEP_MONTHS

    print(f"{'WINDOW train->test':42} | {'best ADX':8} | {'trainPF':7} | {'testPF(chosen)':14} | {'testPF(ADX25)':12} | BEAT?")
    print("-" * 110)
    beats = 0
    total = 0
    for (s, t_end, te_end) in windows:
        # train: pick best ADX by train PF
        best_adx, best_pf = 25, -1.0
        train_pfs = {}
        for a in ADXS:
            pf, _ = run_backtest(STRATEGY[a], to_tr(s, t_end))
            train_pfs[a] = pf
            if pf is not None and pf > best_pf:
                best_pf, best_adx = pf, a
        # test: chosen ADX vs fixed ADX25
        test_chosen, _ = run_backtest(STRATEGY[best_adx], to_tr(t_end, te_end))
        test_25, _ = run_backtest(STRATEGY[25], to_tr(t_end, te_end))
        beat = (test_chosen is not None and test_25 is not None and test_chosen > test_25)
        if test_chosen is not None and test_25 is not None:
            total += 1
            beats += 1 if beat else 0
        label = f"{to_tr(s, t_end).split('-')[0]}->{to_tr(t_end, te_end).split('-')[0]}"
        print(f"{label:42} | {str(best_adx):8} | {str(best_pf):7} | {str(test_chosen):14} | {str(test_25):12} | {'YES' if beat else 'no'}")

    print("-" * 110)
    if total:
        print(f"Train-chosen ADX beat fixed ADX25 on TEST in {beats}/{total} windows "
              f"({100*beats/total:.0f}%).")
        print("Verdict: " + ("ADX>25 is REAL (consistent out-of-sample edge)."
                             if beats / total >= 0.6 else
                             "ADX>25 is OVERFIT (train-chosen ADX does NOT consistently beat ADX25 out-of-sample)."))


if __name__ == "__main__":
    main()
