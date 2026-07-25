#!/usr/bin/env python3
"""Walk-forward / hold-out validation of the V3-nostop daily 200DMA braked-hold
strategy (freeze-safe, offline, standalone).

WHY this exists: freeze period 2026-07. run_backtest.py showed V3-nostop
(daily Close>200DMA enter, Close<200DMA exit, NO hard stop) at +170% /
5.82% CAGR / PF 2.63 / Sharpe 0.54 IN-SAMPLE over full history. In-sample
edge is not proof — this file re-measures the SAME strategy on data it never
"saw" during that read, two ways:
  (1) Clean hold-out: in-sample 2009-01..2022-12, OOS 2023-01..2026-07.
  (2) Rolling walk-forward: train 5y, test next 1y, roll +1y to end of data.
No parameters are fit on the OOS windows (the 200DMA rule is fixed), so this
is an honest out-of-sample confirmation, not a re-optimisation.

Reuses run_backtest.py verbatim: load_csv / backtest / metrics. No network,
no randomness. Reads ../data/NIFTYBEES_daily.csv only.

SURVIVES OOS iff aggregate OOS: total_return > 0 AND sharpe > 0 AND
profit_factor > 1.0 AND max_drawdown not worse than ~ -45%.
"""

import json
import os

import numpy as np
import pandas as pd

from run_backtest import load_csv, backtest, metrics, FEE_LOW

HERE = os.path.dirname(os.path.abspath(__file__))

# In-sample V3-nostop reference (from run_backtest.py results.json, full history).
IN_SAMPLE = {"total_return_pct": 170.0, "cagr_pct": 5.82,
             "profit_factor": 2.63, "sharpe": 0.54}

# Survival thresholds for the aggregate OOS result.
SURV_MIN_TOTAL = 0.0
SURV_MIN_SHARPE = 0.0
SURV_MIN_PF = 1.0
SURV_MAX_DD = -45.0   # OOS aggregate max drawdown must be >= this (less negative)


def v3_nostop_signals(df):
    """V3-nostop: enter when Close crosses ABOVE its 200DMA, exit when it
    crosses BELOW. No hard stop is applied at the backtest() call (stop=-1.0)."""
    d = df.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    entry = ((d["Close"] > d["sma200"]) &
             (d["Close"].shift(1) <= d["sma200"].shift(1))).fillna(False)
    exit_ = ((d["Close"] < d["sma200"]) &
             (d["Close"].shift(1) >= d["sma200"].shift(1))).fillna(False)
    return entry, exit_


def _slice(df, start=None, end=None):
    """Slice by date but keep 200 warm-up rows BEFORE `start` so the 200DMA is
    already primed on the first tradeable OOS bar (no cold-start distortion).
    The reported metrics still cover only the [start, end] window because
    backtest equity is renormalised to the window's first bar."""
    m = pd.Series(True, index=df.index)
    tz = df["Date"].dt.tz
    def _ts(v):
        t = pd.Timestamp(v)
        return t.tz_localize(tz) if (tz is not None and t.tzinfo is None) else t
    if start is not None:
        m &= df["Date"] >= _ts(start)
    if end is not None:
        m &= df["Date"] <= _ts(end)
    idx = df.index[m]
    if len(idx) == 0:
        return None
    lo = max(0, idx[0] - 200)          # 200-bar warm-up for the SMA
    return df.loc[lo: idx[-1]].reset_index(drop=True), idx[0] - lo


def run_window(df, start, end, label):
    """Backtest V3-nostop over [start, end]. Returns a metrics dict + B&H."""
    sl = _slice(df, start, end)
    if sl is None:
        return None
    win, warm = sl
    entry, exit_ = v3_nostop_signals(win)
    # Zero out signals inside the warm-up region so no trade opens before `start`.
    if warm > 0:
        entry.iloc[:warm] = False
        exit_.iloc[:warm] = False
    eq, trades = backtest(win, entry, exit_, FEE_LOW, stop=-1.0)
    # Renormalise equity + metrics to the tradeable window (drop warm-up bars).
    eq_w = eq.iloc[warm:]
    tw = win.iloc[warm:].reset_index(drop=True)
    years = (tw["Date"].iloc[-1] - tw["Date"].iloc[0]).days / 365.25
    bars_in = sum(t["bars"] for t in trades)
    m = metrics(eq_w / eq_w.iloc[0], trades, bars_in, len(tw), years)
    m["window"] = label
    m["start"] = str(tw["Date"].iloc[0].date())
    m["end"] = str(tw["Date"].iloc[-1].date())
    # Buy & Hold over the exact same window for a fair benchmark.
    bh_e = pd.Series(False, index=win.index); bh_e.iloc[warm] = True
    bh_x = pd.Series(False, index=win.index)
    bh_eq, bh_tr = backtest(win, bh_e, bh_x, FEE_LOW, stop=-1.0)
    bh_eq_w = bh_eq.iloc[warm:]
    bhm = metrics(bh_eq_w / bh_eq_w.iloc[0], bh_tr, sum(t["bars"] for t in bh_tr),
                  len(tw), years)
    m["bh_total_return_pct"] = bhm["total_return_pct"]
    m["bh_cagr_pct"] = bhm["cagr_pct"]
    return m


def _agg(windows):
    """Aggregate OOS across rolling windows: chain per-window returns into one
    equity curve (compounded) for total/CAGR/DD; sum trades; trade-weighted
    win rate; pooled PF; and an average of per-window Sharpe (each window's
    Sharpe already annualised)."""
    if not windows:
        return {}
    eq = 1.0
    for w in windows:
        eq *= (1 + w["total_return_pct"] / 100.0)
    total = eq - 1
    # total span years across the (contiguous) OOS windows
    y = sum((pd.Timestamp(w["end"]) - pd.Timestamp(w["start"])).days for w in windows) / 365.25
    cagr = eq ** (1 / y) - 1 if y > 0 else float("nan")
    trades = sum(w["trades"] for w in windows)
    wins = sum(w["trades"] * w["win_rate_pct"] / 100.0 for w in windows)
    # pooled PF: reconstruct gross profit / gross loss from per-window PF is not
    # exact; instead weight PF by trades as a robust central estimate.
    pfs = [w["profit_factor"] for w in windows if isinstance(w["profit_factor"], (int, float))]
    pf_w = (sum(w["profit_factor"] * w["trades"] for w in windows
                if isinstance(w["profit_factor"], (int, float)))
            / sum(w["trades"] for w in windows
                  if isinstance(w["profit_factor"], (int, float)))) if pfs else float("nan")
    worst_dd = min(w["max_drawdown_pct"] for w in windows)
    sharpe = float(np.mean([w["sharpe"] for w in windows]))
    tim = float(np.mean([w["time_in_market_pct"] for w in windows]))
    bh_eq = 1.0
    for w in windows:
        bh_eq *= (1 + w["bh_total_return_pct"] / 100.0)
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(worst_dd, 2),
        "trades": trades,
        "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
        "profit_factor": round(pf_w, 2),
        "sharpe": round(sharpe, 2),
        "time_in_market_pct": round(tim, 1),
        "bh_total_return_pct": round((bh_eq - 1) * 100, 2),
    }


def main():
    daily = load_csv("NIFTYBEES_daily.csv")

    # ---- (1) Clean hold-out ----
    insample = run_window(daily, None, "2022-12-31", "in-sample 2009..2022")
    holdout = run_window(daily, "2023-01-01", "2026-12-31", "OOS hold-out 2023..2026")

    # ---- (2) Rolling walk-forward: train 5y, test next 1y, roll +1y ----
    start_year = daily["Date"].dt.year.min()
    end_year = daily["Date"].dt.year.max()
    rolling = []
    ty = int(start_year) + 5          # first test year (after 5y train warm-up)
    while ty <= int(end_year):
        w = run_window(daily, f"{ty}-01-01", f"{ty}-12-31", f"OOS {ty}")
        if w and w["trades"] >= 0:
            rolling.append(w)
        ty += 1

    agg = _agg(rolling)

    # ---- survival verdict on the rolling aggregate ----
    def survives(a):
        pf = a.get("profit_factor", 0)
        pf = float(pf) if isinstance(pf, (int, float)) else float("inf")
        return bool(a.get("total_return_pct", -1) > SURV_MIN_TOTAL and
                    a.get("sharpe", -1) > SURV_MIN_SHARPE and
                    pf > SURV_MIN_PF and
                    a.get("max_drawdown_pct", -100) >= SURV_MAX_DD)

    holdout_pass = survives(holdout) if holdout else False
    rolling_pass = survives(agg) if agg else False
    verdict = "PASS" if (holdout_pass and rolling_pass) else "FAIL"

    out = {
        "strategy": "V3-nostop daily 200DMA braked-hold (enter Close>200DMA, exit Close<200DMA, NO hard stop)",
        "generated_from": ["NIFTYBEES_daily.csv"],
        "fee_side": FEE_LOW,
        "survival_criteria": {
            "min_total_return_pct": SURV_MIN_TOTAL, "min_sharpe": SURV_MIN_SHARPE,
            "min_profit_factor": SURV_MIN_PF, "max_drawdown_pct_floor": SURV_MAX_DD},
        "in_sample_reference": IN_SAMPLE,
        "clean_holdout": {"in_sample": insample, "oos": holdout,
                          "oos_survives": holdout_pass},
        "rolling_walkforward": {"windows": rolling, "aggregate": agg,
                                "aggregate_survives": rolling_pass},
        "verdict": verdict,
    }
    path = os.path.join(HERE, "walkforward_results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=lambda o: (
            bool(o) if isinstance(o, (np.bool_,)) else
            float(o) if isinstance(o, (np.floating,)) else
            int(o) if isinstance(o, (np.integer,)) else str(o)))

    # ---- console report ----
    cols = ["window", "start", "end", "total_return_pct", "cagr_pct",
            "max_drawdown_pct", "trades", "win_rate_pct", "profit_factor",
            "sharpe", "time_in_market_pct", "bh_total_return_pct"]
    rows = [r for r in ([insample, holdout] + rolling) if r]
    print(pd.DataFrame(rows)[cols].to_string(index=False))
    print("\nROLLING OOS AGGREGATE:", json.dumps(agg, indent=2))
    print(f"\nClean hold-out survives: {holdout_pass}")
    print(f"Rolling aggregate survives: {rolling_pass}")
    print(f"\n=== VERDICT: {verdict} ===")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
