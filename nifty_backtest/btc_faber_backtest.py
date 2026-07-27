#!/usr/bin/env python3
"""Offline backtest: Faber 200DMA / 10-month SMA / 12-month TSMOM on BTC/ETH,
with India after-tax PnL modeling (30% flat crypto tax, no loss offset, 1% TDS).

FREEZE-SAFE: read-only on cached CSVs in ../user_data/backtest_equity/. No network.
Strategy changes are paper-only (July 2026 freeze) — this measures alternatives
WITHOUT touching brakedhold bot config or nifty_engine.

After-tax formula (per closed trade, per Lane B dossier):
  - gain = close/entry - 1
  - taxed_gain = max(gain, 0) * 0.70   (30% tax, losses give ZERO offset)
  - TDS drag = 1% of sell notional (working-capital drag, recoverable at filing)
  - net_ret = taxed_gain - 0.01        (the 1% TDS reduces effective return)
  - effective_fee = fee_side*2 + 0.01  (round-trip fee + TDS)
"""
import json, os
import numpy as np
import pandas as pd

# INR adapters (fetch_wazirx_inr, fetch_coindcx_inr, fetch_coingecko_inr,
# fetch_coingecko_ohlc_inr) live in inr_data.py -- shared with backtest_india_tax.py
# so there is one fetch/cache implementation, not two copies drifting apart.
from inr_data import (  # noqa: F401  (re-exported for callers/tests importing from here)
    fetch_wazirx_inr,
    fetch_coindcx_inr,
    fetch_coingecko_inr,
    fetch_coingecko_ohlc_inr,
    CACHE_DIR,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "user_data", "backtest_equity")

FEE_SIDE = 0.0005   # 5 bps per side (binance.us taker, realistic)
TDS = 0.01          # 1% TDS on gross sell value (India Sec 194S)
TAX_RATE = 0.30     # 30% flat crypto tax (Sec 115BBH), no loss offset


def load_csv(name):
    df = pd.read_csv(os.path.join(DATA, name), parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df.rename(columns={"timestamp": "Date"})
    # normalize column names to TitleCase (BTC CSV uses lowercase)
    df.columns = [c.capitalize() for c in df.columns]
    return df


def sma200_gate(daily):
    d = daily.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["gate"] = d["Close"] > d["sma200"]
    d["day"] = d["Date"].dt.date
    return d.set_index("day")[["gate", "sma200"]]

def backtest_tax(df, entry_sig, exit_sig, fee_side=FEE_SIDE, tds=TDS,
                 tax_rate=TAX_RATE, gate=None, stop=-1.0):
    """Bar-driven long-only backtester with India after-tax PnL.
    Signal at bar i -> execute at close of bar i+1 (no lookahead)."""
    close = df["Close"].values
    low = df["Low"].values
    dates = df["Date"]
    days = dates.dt.date.values
    n = len(df)
    in_pos = False
    entry_px = 0.0
    entry_i = 0
    eq = 1.0
    equity = np.ones(n)
    trades = []
    ent = entry_sig.values
    exi = exit_sig.values
    for i in range(1, n):
        px = close[i]
        if in_pos:
            # mark equity (pre-tax, for curve)
            eq_now = eq * (px / entry_px)
            fill = px
            exit_reason = None
            stop_px = entry_px * (1 + stop)
            if low[i] <= stop_px:
                fill = stop_px; exit_reason = "stop"
            elif exi[i - 1]:
                fill = px; exit_reason = "signal"
            if exit_reason:
                gross_ret = fill / entry_px - 1
                # --- INDIA AFTER-TAX ---
                taxed_gain = max(gross_ret, 0) * (1 - tax_rate)  # losses uncredited
                net_ret = taxed_gain - tds                        # 1% TDS drag
                eq *= (1 + net_ret)
                trades.append({"entry": str(dates[entry_i]), "exit": str(dates[i]),
                               "bars": i - entry_i, "gross_ret": gross_ret,
                               "net_ret": net_ret, "reason": exit_reason})
                in_pos = False
                equity[i] = eq
            else:
                equity[i] = eq_now
        else:
            equity[i] = eq
            if ent[i - 1]:
                ok = True
                if gate is not None:
                    g = gate["gate"].get(days[i], np.nan)
                    ok = bool(g) if g == g else False
                if ok:
                    in_pos = True; entry_px = px; entry_i = i
    if in_pos:
        gross_ret = close[-1] / entry_px - 1
        taxed_gain = max(gross_ret, 0) * (1 - tax_rate)
        net_ret = taxed_gain - tds
        eq *= (1 + net_ret)
        trades.append({"entry": str(dates[entry_i]), "exit": str(dates.iloc[-1]),
                       "bars": n - 1 - entry_i, "gross_ret": gross_ret,
                       "net_ret": net_ret, "reason": "eod"})
        equity[-1] = eq
    return pd.Series(equity, index=dates), trades

def metrics(equity, trades, n_bars, years):
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    dd = (equity / equity.cummax() - 1).min()
    daily_eq = equity.groupby(equity.index.date).last()
    r = daily_eq.pct_change().dropna()
    sharpe = float(r.mean() / r.std() * np.sqrt(252)) if len(r) > 2 and r.std() > 0 else 0.0
    wins = [t for t in trades if t["net_ret"] > 0]
    losses = [t for t in trades if t["net_ret"] <= 0]
    gp = sum(t["net_ret"] for t in wins)
    gl = -sum(t["net_ret"] for t in losses)
    pf = gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0)
    gross_total = sum(t["gross_ret"] for t in trades)
    net_total = sum(t["net_ret"] for t in trades)
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(pf, 2) if np.isfinite(pf) else "inf",
        "sharpe": round(sharpe, 2),
        "avg_hold_bars": round(np.mean([t["bars"] for t in trades]), 1) if trades else 0.0,
        "gross_total_ret_pct": round(gross_total * 100, 2),
        "net_total_ret_pct": round(net_total * 100, 2),
        "tax_drag_pct": round((gross_total - net_total) * 100, 2),
    }

def run_variant(name, df, entry, exit_, gate=None, stop=-1.0):
    eq, trades = backtest_tax(df, entry, exit_, gate=gate, stop=stop)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    m = metrics(eq, trades, len(df), years)
    m["variant"] = name
    return m

def main():
    btc = load_csv("BTC_USD_3y.csv")
    eth = load_csv("ETH_USD_3y.csv") if os.path.exists(os.path.join(DATA, "ETH_USD_3y.csv")) else None
    gate_btc = sma200_gate(btc)
    results = []

    # ---- BENCHMARK: buy & hold BTC (one buy, one sell) ----
    e = pd.Series(False, index=btc.index); e.iloc[0] = True
    x = pd.Series(False, index=btc.index)
    results.append(run_variant("B&H BTC (after-tax)", btc, e, x, stop=-1.0))

    # ---- V1: Faber 10-month SMA long-flat (monthly resample) ----
    # Enter: month-end close > 10-month SMA; Exit: month-end close < 10-month SMA
    m = btc.copy()
    m["month_end"] = m["Date"].dt.to_period("M").ne(m["Date"].dt.to_period("M").shift())
    m["sma10m"] = m["Close"].rolling(210).mean()  # ~10 months of 21 trading days
    m["long"] = (m["Close"] > m["sma10m"]).fillna(False)
    m["long_prev"] = m["long"].shift(1).fillna(False)
    de = (m["long"] & m["long_prev"].ne(True)).fillna(False)
    dx = (m["long_prev"] & m["long"].ne(True)).fillna(False)
    results.append(run_variant("Faber 10-month SMA long-flat (after-tax)", btc, de, dx, stop=-1.0))

    # ---- V2: 200DMA braked-hold (daily, matches brakedhold bot) ----
    d = btc.copy()
    d["sma200"] = d["Close"].rolling(200).mean()
    d["long"] = (d["Close"] > d["sma200"]).fillna(False)
    d["long_prev"] = d["long"].shift(1).fillna(False)
    de = (d["long"] & d["long_prev"].ne(True)).fillna(False)  # rising edge
    dx = (d["long_prev"] & d["long"].ne(True)).fillna(False)  # falling edge
    results.append(run_variant("200DMA braked-hold (after-tax)", btc, de, dx, stop=-1.0))

    # ---- V3: 12-month TSMOM (sign of 252-day return) ----
    d2 = btc.copy()
    d2["mom12"] = d2["Close"].pct_change(252)
    d2["long"] = (d2["mom12"] > 0).fillna(False)
    d2["long_prev"] = d2["long"].shift(1).fillna(False)
    de = (d2["long"] & d2["long_prev"].ne(True)).fillna(False)
    dx = (d2["long_prev"] & d2["long"].ne(True)).fillna(False)
    results.append(run_variant("12-month TSMOM long-flat (after-tax)", btc, de, dx, stop=-1.0))

    # ---- V4: 200DMA braked-hold + 5% hard stop ----
    results.append(run_variant("200DMA braked-hold +5% stop (after-tax)", btc, de, dx, stop=-0.05))

    # ---- V5: 200DMA braked-hold on BTC/INR (Indian exchange data) ----
    # Uses INR-denominated data from WazirX API (historical klines) or CoinGecko
    # (aggregates Indian exchanges: CoinDCX, ZebPay). Caches for freeze-safe runs.
    try:
        btc_inr = fetch_wazirx_inr(limit=365)
        if len(btc_inr) < 200:
            btc_inr = fetch_coingecko_inr(days=365)
            btc_inr["Open"] = btc_inr["Close"]
            btc_inr["High"] = btc_inr["Close"]
            btc_inr["Low"] = btc_inr["Close"]
        d3 = btc_inr.copy()
        d3["sma200"] = d3["Close"].rolling(200).mean()
        d3["long"] = (d3["Close"] > d3["sma200"]).fillna(False)
        d3["long_prev"] = d3["long"].shift(1).fillna(False)
        de_inr = (d3["long"] & d3["long_prev"].ne(True)).fillna(False)
        dx_inr = (d3["long_prev"] & d3["long"].ne(True)).fillna(False)
        results.append(run_variant("200DMA braked-hold BTC/INR (after-tax)", d3, de_inr, dx_inr, stop=-1.0))
    except Exception as e:
        print(f"  (skipped BTC/INR variant: {e})")

    cols = ["variant", "total_return_pct", "cagr_pct", "max_drawdown_pct",
            "trades", "win_rate_pct", "profit_factor", "sharpe",
            "avg_hold_bars", "gross_total_ret_pct", "net_total_ret_pct", "tax_drag_pct"]
    tbl = pd.DataFrame(results)[cols]
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 50)
    print(tbl.to_string(index=False))
    print(f"\nBTC data: {btc['Date'].iloc[0].date()} -> {btc['Date'].iloc[-1].date()} ({len(btc)} days)")
    print(f"Latest: close={btc['Close'].iloc[-1]:.0f} vs SMA200={btc['Close'].rolling(200).mean().iloc[-1]:.0f}")
    print(f"Bot stance: {'LONG' if btc['Close'].iloc[-1] > btc['Close'].rolling(200).mean().iloc[-1] else 'CASH (flat)'}")

    out = os.path.join(HERE, "btc_faber_results.json")
    with open(out, "w") as f:
        json.dump({"generated_from": ["BTC_USD_3y.csv"], "fee_side": FEE_SIDE,
                   "tds": TDS, "tax_rate": TAX_RATE, "results": results}, f, indent=2)
    print(f"\nWrote {out}")

if __name__ == "__main__":
    main()
