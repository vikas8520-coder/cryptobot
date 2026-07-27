#!/usr/bin/env python3
"""
Scalp diagnosis — WHY does ScalpVwap5m lose? Isolates three candidate causes:
  (1) FEES: re-run with --fee 0; if it flips positive, fees are the killer.
  (2) LOGIC: if it still loses at zero fee, the VWAP-band mean-reversion has
      negative gross expectancy (fees only amplify an already-losing core).
  (3) REGIME: compare train vs hold-out PF to see if it's a regime/timing problem.

Freeze-safe: read-only backtesting. Uses the live ScalpVwap5m + config_scalp.json.
Usage: ./.venv/bin/python3 user_data/scalp_diag.py
"""
import subprocess
import re

CONFIG = "config_scalp.json"
STRATEGY = "ScalpVwap5m"
TIMEFRAME = "5m"
TRAIN = "20190101-20260120"
HOLD = "20260120-20260720"
FULL = "20190101-20260720"


def run(fee, timerange):
    cmd = [
        "./.venv/bin/freqtrade", "backtesting",
        "--config", CONFIG, "--strategy", STRATEGY,
        "--timerange", timerange, "--timeframe", TIMEFRAME,
        "--cache", "none", "--fee", str(fee),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    pf = re.search(r"│ Profit factor\s+│\s+([0-9.]+)", out)
    tp = re.search(r"│ Total profit %\s+│\s+([0-9.-]+)%", out)
    tr = re.search(r"│ Total/Daily Avg Trades\s+│\s+(\d+)", out)
    return (float(tp.group(1)) if tp else None,
            float(pf.group(1)) if pf else None,
            int(tr.group(1)) if tr else 0)


def main():
    print(f"{'window':12} | {'fee':5} | {'TotProfit%':11} | {'PF':5} | trades")
    print("-" * 56)
    for label, rng in [("TRAIN", TRAIN), ("HOLD", HOLD), ("FULL", FULL)]:
        for fee in (0.0, 0.001):  # 0.001 = ~0.1%/side realistic
            tp, pf, tr = run(fee, rng)
            print(f"{label:12} | {fee:<5} | {str(tp):11} | {str(pf):5} | {tr}")
    print("-" * 56)
    # verdict
    _, pf_full_real, _ = run(0.001, FULL)
    tp0, pf0, _ = run(0.0, FULL)
    print("VERDICT:")
    if pf0 and pf0 >= 1.0:
        print(f"  Zero-fee PF={pf0} >= 1 -> LOGIC OK, FEES are the killer (drag {tp0}%->neg).")
    else:
        print(f"  Zero-fee PF={pf0} < 1 -> LOGIC is net-negative even free. FEES amplify,")
        print(f"  don't cause, the loss. Strategy has negative gross expectancy; fix or drop.")


if __name__ == "__main__":
    main()
