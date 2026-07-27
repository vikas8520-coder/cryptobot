#!/usr/bin/env python3
"""
OrbLabSwing walk-forward — is the +57.67% full-period number real, or one more fit?

WHY THIS EXISTS (2026-07-24): the DayTradeORB backtest came back -89.99% / PF0.68 /
DD90%, and -62.77% even at fee=0, so the live day-trader is logic-negative, not
fee-poisoned. Three structural defects were isolated (see the OrbLab* strategies):
no first-cross condition, an inverted payoff ratio, and the 23:00 session flush.
Fixing all three flipped the same entry idea to +57.67% / PF1.26 / DD10.34%.

That is EXACTLY the shape of evidence that has already lied to us three times —
TrendBrake4h posted +99.64% full-period and then died at 47% of OOS windows. So the
pre-registered rule from that incident applies unchanged, and this file deliberately
copies walkforward_trendbrake.py's window logic (1y train / 3mo test / 3mo step,
PF>=1.0 bar, 2022 split, >=60% non-2022 positive) so the verdict is directly
comparable to the futures ones. Only strategy/config/timeframe differ.

CONFIG PRECEDENCE (the reason config_lab_orb.json exists): config_daytrade.json pins
stoploss -0.03 + trailing_stop_positive 0.01 / offset 0.02, and freqtrade applies
config OVER strategy attributes. Backtesting a wider-payoff variant against the live
config silently re-tests the ORIGINAL truncating geometry -- that is not a
hypothetical, it happened in this session: OrbLabPayoff first scored byte-identical
to OrbLabFirstCross until the overrides were stripped. config_lab_orb.json is
config_daytrade.json minus those 5 keys, so the strategy's own geometry is scored.

Freeze-safe: read-only backtesting, no live config/strategy/plist is touched.
Usage: ./.venv/bin/python3 user_data/walkforward_orb.py
"""
import subprocess
import re
from datetime import date, timedelta

CONFIG = "config_lab_orb.json"      # daytrade config minus the pinned stop/trail keys
STRATEGY = "OrbLabSwing"
TIMEFRAME = "1h"
PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
TRAIN_MONTHS = 12
TEST_MONTHS = 3
STEP_MONTHS = 3
START = date(2019, 1, 1)            # spot OHLCV for these pairs begins 2019-10-02
END = date(2026, 7, 20)
CRASH_START = date(2022, 1, 1)
CRASH_END = date(2022, 12, 31)
PASS_RATIO = 0.60                   # >=60% of non-2022 OOS windows positive => viable
FEE_ZERO_TIMERANGE = "20190101-20260720"


def run(timerange, extra=None):
    """One backtest -> (profit_factor, total_profit_pct, trades, ran).

    `ran` is False when freqtrade never produced a result table (pre-2019-10 windows
    have no data). Those are excluded rather than scored as losses — padding the
    denominator with untested windows is the bug walkforward_trendbrake.py called out
    in walkforward_futures.py.
    """
    cmd = ["./.venv/bin/freqtrade", "backtesting", "--config", CONFIG,
           "--strategy", STRATEGY, "--timerange", timerange,
           "--timeframe", TIMEFRAME, "--cache", "none", "--pairs"] + PAIRS
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
            int(tr.group(1)) if tr else 0,
            ran)


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
    skipped = []
    print(f"{STRATEGY} walk-forward — {len(wins)} test windows "
          f"({TRAIN_MONTHS}mo train / {TEST_MONTHS}mo test / {STEP_MONTHS}mo step)")
    print(f"{'TEST window':28} | {'PF':5} | {'Tot%':8} | {'trd':4} | crash? | result")
    print("-" * 72)
    for (s, t_end, te_end) in wins:
        tr_str = to_tr(t_end, te_end)
        pf, tp, trades, ran = run(tr_str)
        has_crash = (te_end > CRASH_START) and (t_end < CRASH_END)
        tag = "CRASH" if has_crash else "     "
        if not ran:
            skipped.append(tr_str)
            print(f"{tr_str:28} | {'-':5} | {'-':8} | {'-':4} | {tag:6} | NO DATA (excluded)")
            continue
        # PF is blank when there are no losing trades; trades>0 and profit>0 is a win.
        ok = (pf >= 1.0) if pf is not None else (trades > 0 and tp is not None and tp > 0)
        if has_crash:
            if ok: pos_crash += 1
            else: neg_crash += 1
        else:
            if ok: pos_no_crash += 1
            else: neg_no_crash += 1
        res = "POS" if ok else "neg"
        print(f"{tr_str:28} | {str(pf):5} | {str(tp):8} | {trades:<4} | {tag:6} | {res}")

    print("-" * 72)
    non_crash_total = pos_no_crash + neg_no_crash
    crash_total = pos_crash + neg_crash
    total = non_crash_total + crash_total
    pos_total = pos_no_crash + pos_crash
    if skipped:
        print(f"EXCLUDED (no data): {len(skipped)} of {len(wins)} windows — "
              f"{skipped[0]} .. {skipped[-1]}")
    print(f"SCORED windows: {total}")
    print(f"ALL scored windows: {pos_total}/{total} positive "
          f"({100*pos_total/max(total,1):.0f}%)")
    print(f"Non-2022 windows: {pos_no_crash}/{non_crash_total} positive "
          f"({100*pos_no_crash/max(non_crash_total,1):.0f}%)")
    print(f"2022-touching windows: {pos_crash}/{crash_total} positive "
          f"({100*pos_crash/max(crash_total,1):.0f}%)")

    # ---- FEE ISOLATION ----
    # DayTradeORB lost 62.77% at fee=0, which is what proved it was logic-negative
    # rather than fee-poisoned. Re-run the fixed variant the same way: a breakout that
    # holds trends (~0.27 trades/day) should barely move when fees are removed.
    print("-" * 72)
    pf0, tp0, tr0, _ = run(FEE_ZERO_TIMERANGE, extra=["--fee", "0"])
    print(f"FULL PERIOD fee=0: {tp0}% / PF {pf0} / {tr0} trades"
          f"   (live-fee baseline: +57.67% / PF1.26)")

    print("-" * 72)
    if non_crash_total and pos_no_crash / non_crash_total >= PASS_RATIO:
        print(f"VERDICT: PASSES — >={int(PASS_RATIO*100)}% of non-2022 out-of-sample "
              f"windows are positive. {STRATEGY} is a viable candidate "
              "(promotion still a human decision).")
    else:
        print(f"VERDICT: REJECT — under {int(PASS_RATIO*100)}% of non-2022 "
              "out-of-sample windows are positive. The +57.67% full-period number is "
              "a fit, not an edge. Do NOT promote.")


if __name__ == "__main__":
    main()
