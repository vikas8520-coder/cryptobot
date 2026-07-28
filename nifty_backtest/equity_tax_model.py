#!/usr/bin/env python3
"""
equity_tax_model.py — INDIAN EQUITY tax model for NIFTYBEES strategy variants.

WHY: tax_analysis.py used the crypto/VDA regime (30% + 1% TDS). NIFTYBEES is a
LISTED EQUITY ETF, so the correct regime (user-researched, 2026-07) is:
  - STCG 20.8% (20% + cess) if held <= 365 calendar days
  - LTCG 13%   (12.5% + cess approx) if held  > 365 calendar days
  - STT 0.1% each side (0.2% round trip)
  - other costs (brokerage, exchange, GST, stamp, impact) ~0.2% round trip
  - LOSS OFFSET allowed (equity vs equity) — modeled as a running loss
    carry-forward pool: taxable = max(gain - pool, 0).

Questions answered:
  1) Holding period impact — same trades, STCG vs LTCG bucketing.
  2) Trade frequency vs friction — sweep of SMA-cross speeds + min-hold locks.
  3) Threshold where active trading beats after-tax B&H.

Read-only: reuses run_backtest.py loaders/signals; touches nothing live.
"""
import os, json
import numpy as np
import pandas as pd
import run_backtest as rb

HERE = os.path.dirname(os.path.abspath(__file__))

STCG, LTCG = 0.208, 0.13
STT_SIDE = 0.001          # 0.1% each side
OTHER_RT = 0.002          # ~0.2% round trip (brokerage/GST/stamp/impact)
FEE_SIDE = STT_SIDE + OTHER_RT / 2.0   # 0.2% per side total friction
LTCG_DAYS = 365


def run_engine(df, entry_sig, exit_sig, min_hold_bars=0):
    """Long-only engine (mirrors run_backtest.backtest, no stop). Returns trade
    list with entry/exit prices+dates so tax can be layered per-trade."""
    close = df["Close"].values
    dates = df["Date"].reset_index(drop=True)
    n = len(df)
    ent, exi = entry_sig.values, exit_sig.values
    in_pos, entry_px, entry_i = False, 0.0, 0
    trades = []
    for i in range(1, n):
        if in_pos:
            if exi[i - 1] and (i - entry_i) >= min_hold_bars:
                trades.append((entry_i, i, entry_px, close[i]))
                in_pos = False
        else:
            if ent[i - 1]:
                in_pos, entry_px, entry_i = True, close[i], i
    if in_pos:
        trades.append((entry_i, n - 1, entry_px, close[-1]))
    return trades, dates


def tax_and_compound(trades, dates, ltcg_days=LTCG_DAYS):
    """Apply equity costs+tax per trade with loss carry-forward offset."""
    eq = 1.0
    loss_pool = 0.0           # in return-fraction units of equity at trade time
    n_st = n_lt = 0
    hold_days_list = []
    gross_eq = 1.0
    for (ei, xi, epx, xpx) in trades:
        gross = xpx / epx
        gross_eq *= gross
        net = gross * (1 - FEE_SIDE) ** 2        # costs both sides
        gain = net - 1.0
        hd = (dates[xi] - dates[ei]).days
        hold_days_list.append(hd)
        if gain > 0:
            taxable = max(gain - loss_pool, 0.0)
            loss_pool = max(loss_pool - gain, 0.0)
            rate = LTCG if hd > ltcg_days else STCG
            if hd > ltcg_days: n_lt += 1
            else: n_st += 1
            mult = 1.0 + gain - rate * taxable
        else:
            loss_pool += -gain
            mult = net
        eq *= mult
    return {"eq": eq, "gross_eq": gross_eq, "n_st": n_st, "n_lt": n_lt,
            "avg_hold_days": float(np.mean(hold_days_list)) if hold_days_list else 0,
            "med_hold_days": float(np.median(hold_days_list)) if hold_days_list else 0,
            "trades": len(trades)}


def summarize(name, trades, dates, years):
    r = tax_and_compound(trades, dates)
    pre = r["gross_eq"] * (1 - FEE_SIDE) ** (2 * len(trades))  # cost-only, pre-tax
    def cagr(x): return (x ** (1 / years) - 1) * 100 if x > 0 else -100.0
    return {"variant": name, "trades": r["trades"],
            "avg_hold_days": round(r["avg_hold_days"], 1),
            "stcg_trades": r["n_st"], "ltcg_trades": r["n_lt"],
            "pretax_total_pct": round((r["gross_eq"] - 1) * 100, 1),
            "aftercost_total_pct": round((pre - 1) * 100, 1),
            "aftertax_total_pct": round((r["eq"] - 1) * 100, 1),
            "aftertax_cagr_pct": round(cagr(r["eq"]), 2)}


def sma_signals(d, fast, slow):
    f = d["Close"].rolling(fast).mean(); s = d["Close"].rolling(slow).mean()
    e = ((f > s) & (f.shift(1) <= s.shift(1))).fillna(False)
    x = ((f < s) & (f.shift(1) >= s.shift(1))).fillna(False)
    return e, x


def main():
    daily = rb.load_csv("NIFTYBEES_daily.csv").reset_index(drop=True)
    years = (daily["Date"].iloc[-1] - daily["Date"].iloc[0]).days / 365.25
    out = []

    # --- B&H benchmark (single trade, all LTCG) ---
    bh_trades = [(0, len(daily) - 1, daily["Close"].iloc[0], daily["Close"].iloc[-1])]
    out.append(summarize("B&H (LTCG 13%)", bh_trades, daily["Date"], years))

    # --- Q2: trade-frequency sweep (SMA cross speeds) ---
    d = daily.copy()
    sweeps = [(5, 20, "fast 5/20"), (10, 50, "10/50"), (20, 100, "20/100"),
              (50, 200, "golden 50/200"), (100, 300, "slow 100/300")]
    for f, s, label in sweeps:
        e, x = sma_signals(d, f, s)
        tr, dt = run_engine(d, e, x)
        out.append(summarize(f"SMA {label}", tr, dt, years))

    # V3-nostop (price x 200DMA) — the migrated config
    d["sma200"] = d["Close"].rolling(200).mean()
    de = ((d["Close"] > d["sma200"]) & (d["Close"].shift(1) <= d["sma200"].shift(1))).fillna(False)
    dx = ((d["Close"] < d["sma200"]) & (d["Close"].shift(1) >= d["sma200"].shift(1))).fillna(False)
    tr, dt = run_engine(d, de, dx)
    out.append(summarize("V3 price x 200DMA", tr, dt, years))

    # --- Q1: same V3 signal, forced MIN-HOLD locks (holding-period impact) ---
    for mh in (63, 126, 252, 380):   # ~3m, 6m, 12m, >LTCG threshold
        tr, dt = run_engine(d, de, dx, min_hold_bars=mh)
        out.append(summarize(f"V3 min-hold {mh} bars (~{int(mh*365/252)}d)", tr, dt, years))

    tbl = pd.DataFrame(out)
    pd.set_option("display.width", 250)
    print(tbl.to_string(index=False))

    # --- Q3: breakeven math (analytic) ---
    print("\n--- Breakeven per-trade edge needed (costs 0.4% RT + tax on gain) ---")
    bh = out[0]["aftertax_cagr_pct"]
    print(f"After-tax B&H CAGR to beat: {bh}% over {years:.1f}y")
    for tpy in (1, 2, 4, 6, 12, 26):
        # required per-trade after-tax multiplier m s.t. m^tpy - 1 = bh_cagr
        m = (1 + bh / 100) ** (1 / tpy)
        for rate, lbl in ((STCG, "STCG"), (LTCG, "LTCG")):
            # solve gross g: (g*(1-fee)^2 - 1)*(1-rate) + 1 = m  (gain case)
            g = ((m - 1) / (1 - rate) + 1) / (1 - FEE_SIDE) ** 2
            print(f"  {tpy:>2} trades/yr [{lbl}]: need {100*(g-1):+.2f}% gross per trade")

    with open(os.path.join(HERE, "equity_tax_model_results.json"), "w") as fjs:
        json.dump({"regime": {"stcg": STCG, "ltcg": LTCG, "stt_side": STT_SIDE,
                              "other_rt": OTHER_RT, "ltcg_days": LTCG_DAYS},
                   "years": round(years, 1), "results": out}, fjs, indent=2)
    print("\nWrote equity_tax_model_results.json")


if __name__ == "__main__":
    main()
