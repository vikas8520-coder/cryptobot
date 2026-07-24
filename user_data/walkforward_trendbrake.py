#!/usr/bin/env python3
"""
TrendBrake4h walk-forward — is the +99.64% single-backtest real, or one lucky fit?

WHY THIS EXISTS (2026-07-23): the futures walk-forward killed TrendFollowLS2 (8/23
non-2022 OOS windows positive = 35% => BROKEN, the 2022 crash was an alibi). Its
replacement prototype TrendBrake4h then posted +99.64% / PF1.85 / DD13.52% on ONE
full-period backtest (2018-2026). A single full-period number is exactly the
evidence that already lied to us about scalp and futures, so the promotion rule
from that incident applies unchanged: no live promotion without >=60% of
out-of-sample windows positive.

Window logic is copied from walkforward_futures.py deliberately (1y train /
3mo test / 3mo step, 2018->2026, 4h) so the two verdicts are directly comparable —
same windows, same PF>=1.0 bar, same 2022 split. Only the strategy differs.

CONFIG PRECEDENCE (the whole reason this file is not walkforward_futures.py with a
renamed constant): config_futures.json pins stoploss -0.08 AND trailing_stop true,
and freqtrade applies config OVER strategy attributes. Backtesting TrendBrake4h
against the bare live config tests a trailing-stop strategy that merely resembles a
brake. The second -c user_data/prototype_overlay_futures.json restores
stoploss -0.99 + trailing_stop false so the PURE brake is what gets scored.

Freeze-safe: read-only backtesting, no live config/strategy/plist is touched.
Usage: ./.venv/bin/python3 user_data/walkforward_trendbrake.py
"""
import subprocess
import re
from datetime import date, timedelta

CONFIG = "config_futures.json"
OVERLAY = "user_data/prototype_overlay_futures.json"   # defeats the pinned stop/trail
STRATEGY = "TrendBrake4h"
TIMEFRAME = "4h"
TRAIN_MONTHS = 12
TEST_MONTHS = 3
STEP_MONTHS = 3
START = date(2018, 1, 1)
END = date(2026, 1, 1)
CRASH_START = date(2022, 1, 1)
CRASH_END = date(2022, 12, 31)
PASS_RATIO = 0.60          # >=60% of non-2022 OOS windows positive => viable
FEE_ZERO_TIMERANGE = "20180101-20260101"


def run(timerange, extra=None):
    """One backtest -> (profit_factor, total_profit_pct, trades, ran).

    `ran` is False when freqtrade never produced a result table — futures OHLCV
    only begins 2019-12-28 (LTC) / 2020-09-23 (AVAX), so every pre-2020 window
    dies with "No data found. Terminating.". walkforward_futures.py did not make
    this distinction: a no-data window parsed to PF=None and was scored as a LOSS,
    which silently pads the denominator with windows the strategy was never
    actually tested in. Those windows are excluded here and reported separately.
    """
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
    ran = tp is not None            # a result table was printed at all
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
    print(f"TrendBrake4h walk-forward — {len(wins)} test windows "
          f"({TRAIN_MONTHS}mo train / {TEST_MONTHS}mo test / {STEP_MONTHS}mo step)")
    print(f"{'TEST window':28} | {'PF':5} | {'Tot%':8} | {'trd':4} | crash? | result")
    print("-" * 72)
    for (s, t_end, te_end) in wins:
        tr_str = to_tr(t_end, te_end)
        pf, tp, trades, ran = run(tr_str)
        has_crash = (te_end > CRASH_START) and (t_end < CRASH_END)
        tag = "CRASH" if has_crash else "     "
        if not ran:
            # No futures OHLCV for this span — not evidence either way.
            skipped.append(tr_str)
            print(f"{tr_str:28} | {'-':5} | {'-':8} | {'-':4} | {tag:6} | NO DATA (excluded)")
            continue
        # PF is blank in freqtrade's table when there are no losing trades; with
        # trades>0 and profit>0 that is a win, not a parse failure.
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
        print(f"EXCLUDED (no futures data): {len(skipped)} of {len(wins)} windows — "
              f"{skipped[0]} .. {skipped[-1]}")
    print(f"SCORED windows: {total}")
    print(f"ALL scored windows: {pos_total}/{total} positive "
          f"({100*pos_total/max(total,1):.0f}%)")
    print(f"Non-2022 windows: {pos_no_crash}/{non_crash_total} positive "
          f"({100*pos_no_crash/max(non_crash_total,1):.0f}%)")
    print(f"2022-touching windows: {pos_crash}/{crash_total} positive "
          f"({100*pos_crash/max(crash_total,1):.0f}%)")

    # ---- FEE ISOLATION ----
    # The scalp post-mortem showed fees can BE the whole result. A brake that holds
    # trends should barely notice them (~9-20 trades/yr), so if fee=0 is wildly
    # better than the +99.64% live-fee number, the edge is thinner than it looks.
    print("-" * 72)
    pf0, tp0, tr0, _ = run(FEE_ZERO_TIMERANGE, extra=["--fee", "0"])
    print(f"FULL PERIOD fee=0: {tp0}% / PF {pf0} / {tr0} trades"
          f"   (live-fee baseline: +99.64% / PF1.85)")

    print("-" * 72)
    if non_crash_total and pos_no_crash / non_crash_total >= PASS_RATIO:
        print(f"VERDICT: PASSES — >={int(PASS_RATIO*100)}% of non-2022 out-of-sample "
              "windows are positive. TrendBrake4h is a viable live candidate "
              "(promotion still a human decision).")
    else:
        print(f"VERDICT: REJECT — under {int(PASS_RATIO*100)}% of non-2022 "
              "out-of-sample windows are positive. The +99.64% full-period number is "
              "a fit, not an edge. Do NOT promote.")


if __name__ == "__main__":
    main()
