#!/usr/bin/env python3
"""
Futures walk-forward — is TrendFollowLS2 broken or just caught in 2022?

Tests the BASE futures strategy (TrendFollowLS2, no ADX sweep) across rolling
windows. For each: train 1y / test 3mo, step 3mo, 2018->2026, 4h, LINK/AVAX/LTC/ADA.
Reports per-window PF + whether the window includes the 2022 crash. Aggregate:
how many OUT-OF-SAMPLE windows are positive, split by "contains 2022" vs not.

If non-2022 windows are mostly positive -> regime (unlucky). If mostly negative
everywhere -> broken (like scalp).

Freeze-safe: read-only backtesting. Uses live config_futures.json + TrendFollowLS2.
Usage: ./.venv/bin/python3 user_data/walkforward_futures.py
"""
import subprocess
import re
from datetime import date, timedelta

CONFIG = "config_futures.json"
STRATEGY = "TrendFollowLS2"
TIMEFRAME = "4h"
TRAIN_MONTHS = 12
TEST_MONTHS = 3
STEP_MONTHS = 3
START = date(2018, 1, 1)
END = date(2026, 1, 1)
CRASH_START = date(2022, 1, 1)
CRASH_END = date(2022, 12, 31)


def run(timerange):
    cmd = ["./.venv/bin/freqtrade", "backtesting", "--config", CONFIG,
           "--strategy", STRATEGY, "--timerange", timerange,
           "--timeframe", TIMEFRAME, "--cache", "none"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    pf = re.search(r"│ Profit factor\s+│\s+([0-9.]+)", out)
    tp = re.search(r"│ Total profit %\s+│\s+([0-9.-]+)%", out)
    return (float(pf.group(1)) if pf else None,
            float(tp.group(1)) if tp else None)


def to_tr(s, e):
    return f"{s.strftime('%Y%m%d')}-{e.strftime('%Y%m%d')}"


def main():
    wins = []
    s = START
    while True:
        t_end = s + timedelta(days=TRAIN_MONTHS * 30)
        te_end = t_end + timedelta(days=TEST_MONTHS * 30)
        if te_end > END:
            break
        wins.append((s, t_end, te_end))
        s = s + timedelta(days=STEP_MONTHS * 30)

    pos_no_crash = pos_crash = neg_no_crash = neg_crash = 0
    print(f"{'TEST window':28} | {'PF':5} | {'Tot%':8} | crash? | result")
    print("-" * 64)
    for (s, t_end, te_end) in wins:
        pf, tp = run(to_tr(t_end, te_end))
        has_crash = (te_end > CRASH_START) and (t_end < CRASH_END)
        ok = pf is not None and pf >= 1.0
        if has_crash:
            (pos_crash if ok else neg_crash)
            if ok: pos_crash += 1
            else: neg_crash += 1
        else:
            if ok: pos_no_crash += 1
            else: neg_no_crash += 1
        tag = "CRASH" if has_crash else "     "
        res = "POS" if ok else "neg"
        print(f"{to_tr(t_end, te_end):28} | {str(pf):5} | {str(tp):8} | {tag:6} | {res}")

    print("-" * 64)
    non_crash_total = pos_no_crash + neg_no_crash
    crash_total = pos_crash + neg_crash
    print(f"Non-2022 windows: {pos_no_crash}/{non_crash_total} positive "
          f"({100*pos_no_crash/max(non_crash_total,1):.0f}%)")
    print(f"2022-touching windows: {pos_crash}/{crash_total} positive "
          f"({100*pos_crash/max(crash_total,1):.0f}%)")
    if non_crash_total and pos_no_crash / non_crash_total >= 0.6:
        print("VERDICT: REGIME — futures is fine outside 2022; the negative full-period "
              "is the 2022 crash. NOT broken.")
    else:
        print("VERDICT: BROKEN — even non-2022 windows are mostly negative. Like scalp, "
              "the logic is net-negative. Redesign or drop.")


if __name__ == "__main__":
    main()
