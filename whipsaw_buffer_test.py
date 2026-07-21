#!/usr/bin/env python3
"""
whipsaw_buffer_test.py — does a hysteresis buffer on the 200-day brake actually help?

Idea: instead of flipping on ANY cross of the SMA, require price to break it by a margin:
  in cash -> go long only when close > SMA*(1+b)
  in mkt  -> go cash only when close < SMA*(1-b)
The ±b% dead-zone should cut whipsaw (fewer switches) in choppy markets. The honest
question: does it reduce switches WITHOUT hurting max drawdown or risk-adjusted return —
and does it hold across MULTIPLE assets (robust) or only where it was cherry-picked?

Buffer 0.0 = today's rule (baseline). Reuses regime_hold_backtest's data + engine
(daily, 2021-2026, 0.1% switch fees, point-in-time, no lookahead).
"""
import warnings

import numpy as np
import pandas as pd

import regime_hold_backtest as rb

warnings.filterwarnings("ignore")

BUFFERS = [0.0, 0.01, 0.02, 0.03, 0.05]


def buffered_pos(price: pd.Series, buffer: float) -> pd.Series:
    """0/1 exposure with hysteresis around the 200-day SMA."""
    sma = price.rolling(200).mean().values
    p = price.values
    pos = np.zeros(len(p))
    state = 0
    for i in range(len(p)):
        if np.isnan(sma[i]):
            continue
        if state == 0 and p[i] > sma[i] * (1 + buffer):
            state = 1
        elif state == 1 and p[i] < sma[i] * (1 - buffer):
            state = 0
        pos[i] = state
    return pd.Series(pos, index=price.index)


def main():
    closes = rb.daily_closes()
    assets = {
        "BTC": closes["BTC"], "ETH": closes["ETH"], "SOL": closes["SOL"],
        "XRP": closes["XRP"], "Basket": rb.basket(closes),
    }

    print("WHIPSAW BUFFER TEST — 200-day brake with a ±b% dead-zone")
    print("daily 2021-2026 · 0.1% fees · point-in-time · buffer 0.0 = today's rule\n")

    # collect for a cross-asset verdict
    agg = {b: {"switches": [], "dd": [], "calmar": [], "ret": []} for b in BUFFERS}

    for name, price in assets.items():
        print(f"── {name} " + "─" * (58 - len(name)))
        print(f"   {'buffer':>7}{'return':>9}{'maxDD':>8}{'Calmar':>8}{'switches':>9}{'%inMkt':>8}")
        base_sw = None
        for b in BUFFERS:
            pos = buffered_pos(price, b)
            eq, sw, tim = rb.run_position(price, pos)
            m = rb.metrics(eq, sw, tim)
            if b == 0.0:
                base_sw = sw
            tag = ""
            if b == 0.0:
                tag = "  <- baseline"
            print(f"   {b*100:>6.0f}%{m['total']*100:>8.0f}%{m['maxdd']*100:>7.0f}%"
                  f"{m['calmar']:>8.2f}{sw:>9d}{m['tim']*100:>7.0f}%{tag}")
            agg[b]["switches"].append(sw)
            agg[b]["dd"].append(m["maxdd"])
            agg[b]["calmar"].append(m["calmar"])
            agg[b]["ret"].append(m["total"])
        print()

    # cross-asset averages vs baseline
    print("═" * 62)
    print("CROSS-ASSET AVERAGE (the robustness check — is it consistent?)")
    print(f"   {'buffer':>7}{'avg ret':>9}{'avg DD':>8}{'avgCalmar':>10}{'avg switches':>13}")
    b0 = {k: np.mean(agg[0.0][k]) for k in ("switches", "dd", "calmar", "ret")}
    for b in BUFFERS:
        sw = np.mean(agg[b]["switches"])
        dd = np.mean(agg[b]["dd"])
        cal = np.mean(agg[b]["calmar"])
        ret = np.mean(agg[b]["ret"])
        d_sw = (sw - b0["switches"]) / b0["switches"] * 100 if b0["switches"] else 0
        print(f"   {b*100:>6.0f}%{ret*100:>8.0f}%{dd*100:>7.0f}%{cal:>10.2f}"
              f"{sw:>9.0f} ({d_sw:+.0f}%)")

    print("\nRead: a buffer EARNS its place only if it cuts switches meaningfully AND keeps")
    print("maxDD ~equal-or-better AND Calmar ~equal-or-better, CONSISTENTLY across assets.")
    print("If the 'best' buffer differs wildly per asset, it's noise — reject.")


if __name__ == "__main__":
    main()
