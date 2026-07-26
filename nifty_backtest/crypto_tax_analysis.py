#!/usr/bin/env python3
"""
crypto_tax_analysis.py — after-tax reality check for the crypto bots (freeze-safe).

WHY: nifty_backtest/tax_analysis.py proved India's tax regime (30% flat on gains
+ 1% TDS on EVERY sell + NO loss offset) turns our best Nifty strategy from +170%
pre-tax to -26% after-tax. Crypto is taxed the SAME way in India (Section 115BBH).
Vikas asked to apply the identical test to the two surviving frozen-test bots,
brakedhold and spot.

TWO DIFFERENT EVIDENCE TYPES, handled distinctly (honesty matters):
  1. SPOT — REALIZED LEDGER. tradesv3_trendfollow.sqlite has 27 actual closed
     trades with open_rate/close_rate/amount/fees. We tax each REAL trade directly:
     per trade, exit_value = amount*close_rate; TDS = 1% of exit_value; gain tax
     = 30% of max(realized_pnl_net_of_fees, 0); net = pnl - TDS - gain_tax. No
     simulation, no look-ahead — this is what actually happened to the paper wallet.
  2. BRAKEDHOLD — BACKTEST CLAIM. Its live ledger has 0 closed trades (1 open TRX),
     so we cannot tax-test realized crypto results yet. Instead we test its advertised
     edge ("+1219% PF11", a backtest) the SAME way as Nifty: rebuild its exact logic
     (daily close >200DMA -> long, <200DMA -> cash; no stop, no ROI) across its 12-coin
     basket on daily yfinance data, equal-weighted, pre-tax vs after-tax.

REUSE: imports backtest_tax() from tax_analysis.py so the execution engine AND the
tax model are byte-for-byte identical to the Nifty analysis. Read-only: reads the
sqlite ledger + yfinance (network ok, no bot touched).

NOTE: yfinance crypto history is ~5y (1826 daily rows), shorter than Nifty's 17.6y.
So crypto CAGR is NOT comparable to Nifty CAGR — compare crypto PRE-tax vs POST-tax
within this script, and treat the absolute CAGR as a 5y-window figure only.
"""
import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tax_analysis as ta          # reuse backtest_tax() + the tax model
import run_backtest as rb          # reuse load_csv/metrics semantics

TAX_RATE = ta.TAX_RATE             # 0.30
TDS = ta.TDS                       # 0.01
FEE_SIDE = 0.001                   # ~0.1% per side (binance standard, as configured)
BRAKE_BASKET = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC",
                "DOGE", "LINK", "BNB", "AVAX", "DOT", "TRX"]


# ---------------------------------------------------------------------------
# 1. SPOT — realized ledger, taxed per real trade
# ---------------------------------------------------------------------------
def spot_realized_after_tax(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT pair, open_rate, close_rate, amount, close_profit_abs, "
        "fee_open_cost, fee_close_cost, exit_reason FROM trades WHERE is_open=0 "
        "ORDER BY close_date"
    ).fetchall()
    con.close()
    if not rows:
        return None
    trades = []
    pre_total = 0.0     # net of broker fees (close_profit_abs) pre-tax
    post_total = 0.0    # after India tax
    for pair, o, c, amt, pnl, fo, fc, reason in rows:
        exit_value = amt * c
        tds = TDS * exit_value
        gain_tax = TAX_RATE * pnl if pnl > 0 else 0.0     # no loss offset
        net = pnl - tds - gain_tax
        pre_total += pnl
        post_total += net
        trades.append({"pair": pair, "pnl": pnl, "tds": tds,
                       "gain_tax": gain_tax, "net": net, "reason": reason})
    return {"n": len(trades), "pre": pre_total, "post": post_total,
            "trades": trades}


# ---------------------------------------------------------------------------
# 2. BRAKEDHOLD — 200DMA brake backtest across the basket, pre vs post tax
# ---------------------------------------------------------------------------
def fetch_daily(symbol):
    df = yf.Ticker(f"{symbol}-USD").history(period="5y", interval="1d")
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df = df.reset_index()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    return df.sort_values("Date").reset_index(drop=True)


def brake_signals(df):
    c = df["Close"]
    sma = c.rolling(200).mean()
    entry = (c > sma) & (c.shift(1) <= sma.shift(1))
    exit_ = (c < sma) & (c.shift(1) >= sma.shift(1))
    return entry.fillna(False), exit_.fillna(False)


def basket_backtest(symbols, fee_side=FEE_SIDE, tax_on=True):
    """Equal-weighted basket of 200DMA-brake long-only positions. Each coin's
    equity curve starts at 1.0; basket = simple mean of curves (equal weight).
    Returns the basket equity Series + aggregate trade count."""
    curves = []
    total_trades = 0
    for s in symbols:
        try:
            df = fetch_daily(s)
        except Exception as e:
            print(f"  skip {s}: {type(e).__name__}: {e}")
            continue
        if len(df) < 210:
            print(f"  skip {s}: only {len(df)} rows (<210 for 200DMA)")
            continue
        de, dx = brake_signals(df)
        eq, trades = ta.backtest_tax(df, de, dx, fee_side,
                                     stop=-1.0, max_hold_bars=0,
                                     gate=None, tax_on=tax_on)
        curves.append(eq)
        total_trades += len(trades)
    if not curves:
        return None, 0
    # align on common date index (intersect) then mean
    common = curves[0].index
    for c in curves[1:]:
        common = common.intersection(c.index)
    mat = pd.DataFrame({i: c.reindex(common).ffill() for i, c in enumerate(curves)})
    basket = mat.mean(axis=1)
    return basket, total_trades


def metrics_from_equity(equity):
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1]) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    return {"total_return_pct": round(total * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "max_drawdown_pct": round(dd * 100, 2)}


def main():
    print("=" * 72)
    print("CRYPTO AFTER-TAX ANALYSIS (India: 30% on gains, 1% TDS/sell, no loss offset)")
    print("=" * 72)

    # ---- SPOT: realized ledger ----
    print("\n[1] SPOT — REALIZED LEDGER (27 actual closed paper trades)\n")
    sp = spot_realized_after_tax(
        os.path.join(os.path.dirname(HERE), "tradesv3_trendfollow.sqlite"))
    if sp:
        wallet = 1000.0   # spot bot started at $1000 (balance ~978 now pre-tax)
        print(f"  closed trades     : {sp['n']}")
        print(f"  pre-tax  P&L      : ${sp['pre']:+.2f}  ({sp['pre']/wallet*100:+.2f}% of wallet)")
        print(f"  after-tax P&L     : ${sp['post']:+.2f}  ({sp['post']/wallet*100:+.2f}% of wallet)")
        print(f"  tax drag          : ${(sp['pre']-sp['post']):+.2f}")
        # breakdown by reason
        by_reason = {}
        for t in sp["trades"]:
            by_reason.setdefault(t["reason"], [0, 0.0, 0.0])
            by_reason[t["reason"]][0] += 1
            by_reason[t["reason"]][1] += t["pnl"]
            by_reason[t["reason"]][2] += t["net"]
        print("  by exit reason    : reason / n / pre / post")
        for r, (n, pre, post) in sorted(by_reason.items()):
            print(f"    {r:14s} {n:3d}  ${pre:+.2f}  ${post:+.2f}")
    else:
        print("  no closed trades in spot ledger")

    # ---- BRAKEDHOLD: 200DMA basket backtest ----
    print("\n[2] BRAKEDHOLD — 200DMA-brake basket backtest (12 coins, daily, 5y)\n")
    pre_eq, pre_n = basket_backtest(BRAKE_BASKET, tax_on=False)
    post_eq, post_n = basket_backtest(BRAKE_BASKET, tax_on=True)
    if pre_eq is not None and post_eq is not None:
        pm = metrics_from_equity(pre_eq)
        qm = metrics_from_equity(post_eq)
        print(f"  coins resolved     : {len(pre_eq) and 'see skips above'}")
        print(f"  total round-trips  : {pre_n} (pre) / {post_n} (post)")
        print(f"  PRE-TAX            : total {pm['total_return_pct']:+.2f}%  "
              f"CAGR {pm['cagr_pct']:+.2f}%  maxDD {pm['max_drawdown_pct']:.2f}%")
        print(f"  AFTER-TAX          : total {qm['total_return_pct']:+.2f}%  "
              f"CAGR {qm['cagr_pct']:+.2f}%  maxDD {qm['max_drawdown_pct']:.2f}%")
        verdict = "VIABLE after tax" if qm["total_return_pct"] > 0 else "NEGATIVE after tax"
        print(f"  VERDICT            : {verdict}")
    else:
        print("  basket backtest produced no curves")

    # save
    out = {
        "tax_rate": TAX_RATE, "tds": TDS, "fee_side": FEE_SIDE,
        "spot_realized": sp,
    }
    with open(os.path.join(HERE, "crypto_tax_analysis_results.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("\nWrote nifty_backtest/crypto_tax_analysis_results.json")


if __name__ == "__main__":
    main()
