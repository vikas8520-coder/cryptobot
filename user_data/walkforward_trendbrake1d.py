#!/usr/bin/env python3
"""
TrendBrake1dFutures walk-forward — the last gate before it could go live.

Same promotion rule as the other walk-forwards: >=60% of NON-2022 out-of-sample
windows positive => viable; else REJECT. Window logic adapted for 1d data
(2y train / 6mo test / 6mo step, 2018->2026).

CONFIG PRECEDENCE: config_futures.json pins stoploss -0.08 + trailing_stop true,
which freqtrade applies OVER strategy attributes. The second -c overlay restores
stoploss -0.99 + trailing_stop false so the PURE brake is scored.

Freeze-safe: read-only backtesting. Usage: ./.venv/bin/python3 user_data/walkforward_trendbrake1d.py
"""
import subprocess
import re
from datetime import date, timedelta

CONFIG = "config_futures.json"
OVERLAY = "user_data/prototype_overlay_futures.json"
STRATEGY = "TrendBrake1dFutures"
TIMEFRAME = "1d"
TRAIN_YEARS = 2
TEST_MONTHS = 6
STEP_MONTHS = 6
START = date(2018, 1, 1)
END = date(2026, 1, 1)
CRASH_START = date(2022, 1, 1)
CRASH_END = date(2022, 12, 31)
PASS_RATIO = 0.60


def run(timerange, extra=None):
    cmd = ["./.venv/bin/freqtrade", "backtesting", "--config", CONFIG,
           "--config", OVERLAY, "--strategy", STRATEGY,
           "--timerange", timerange, "--timeframe", TIMEFRAME, "--cache", "none"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = p.stdout + p.stderr
    pf = re.search(r"│ Profit factor\s+│\s+([0-9.]+)", out)
    tp = re.search(r"│ Total profit %\s+│\s+([0-9.-]+)%", out)
    tr = re.search(r"│ Total/Daily Avg Trades\s+│\s+([0-9]+)", out)
    ran = tp is not None
    return (float(pf.group(1)) if pf else None,
            float(tp.group(1)) if tp else None,
            int(tr.group(1)) if tr else 0, ran)


def to_tr(s, e):
    return f"{s.strftime('%Y%m%d')}-{e.strftime('%Y%m%d')}"


def main():
    wins = []
    s = START
    while True:
        t_end = s + timedelta(days=TRAIN_YEARS * 365)
        te_end = t_end + timedelta(days=TEST_MONTHS * 30)
        if te_end > END:
            break
        wins.append((s, t_end, te_end))
        s = s + timedelta(days=STEP_MONTHS * 30)

    pos_no_crash = pos_crash = neg_no_crash = neg_crash = 0
    print(f"TrendBrake1dFutures walk-forward — {len(wins)} test windows "
          f"({TRAIN_YEARS}y train / {TEST_MONTHS}mo test / {STEP_MONTHS}mo step)")
    print(f"{'TEST window':28} | {'PF':5} | {'Tot%':8} | {'trd':4} | crash? | result")
    print("-" * 72)
    for (s, t_end, te_end) in wins:
        tr_str = to_tr(t_end, te_end)
        pf, tp, tr, ran = run(tr_str)
        if not ran:
            print(f"{tr_str:28} | {'-':5} | {'-':8} | {'-':4} |       | NO DATA (excluded)")
            continue
        has_crash = (te_end > CRASH_START) and (t_end < CRASH_END)
        ok = pf is not None and pf >= 1.0
        if has_crash:
            if ok: pos_crash += 1
            else: neg_crash += 1
        else:
            if ok: pos_no_crash += 1
            else: neg_no_crash += 1
        tag = "CRASH" if has_crash else "     "
        res = "POS" if ok else "neg"
        print(f"{tr_str:28} | {str(pf):5} | {str(tp):8} | {tr:4} | {tag:6} | {res}")

    print("-" * 72)
    non_crash_total = pos_no_crash + neg_no_crash
    crash_total = pos_crash + neg_crash
    print(f"Non-2022 windows: {pos_no_crash}/{non_crash_total} positive "
          f"({100*pos_no_crash/max(non_crash_total,1):.0f}%)")
    print(f"2022-touching windows: {pos_crash}/{crash_total} positive "
          f"({100*pos_crash/max(crash_total,1):.0f}%)")
    if non_crash_total and pos_no_crash / non_crash_total >= PASS_RATIO:
        print(f"VERDICT: PASSES — >=60% of non-2022 OOS windows positive. "
              f"TrendBrake1dFutures is a viable candidate for live promotion.")
    else:
        print(f"VERDICT: REJECT — under 60% of non-2022 OOS windows positive. "
              f"Do NOT promote.")


if __name__ == "__main__":
    main()
