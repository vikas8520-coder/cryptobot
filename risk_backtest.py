#!/usr/bin/env python3
"""
risk_backtest.py — prove (or disprove) the risk brain on 5.5 years of data.

The experiment: feed the SAME trade signals into two portfolio simulators that
differ ONLY in the risk overlay, so any difference in drawdown is caused by risk
management, not by luckier entries.

  V1 (today):     flat $100 stake · max 3 positions (count cap) · fixed 10% breaker
  V2 (riskbrain): vol-scaled stake · correlation-adjusted $ budget · adaptive breaker

Everything volatility/correlation is POINT-IN-TIME (trailing windows, shifted so a
bar never sees its own future) — no lookahead bias.

Signals = the live trend-following idea: long when price>EMA20>EMA50>EMA100 and
ADX>25; exit when close<EMA50 or the wide -27% stop hits (matches the live bot).
Long-only spot, 6 coins, $1000 start. Run: ./.venv/bin/python risk_backtest.py
"""
from __future__ import annotations
import glob
import os
import warnings

import numpy as np
import pandas as pd
import talib

warnings.filterwarnings("ignore")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "user_data", "data", "binance_vision")
START_CASH = 1000.0
FEE = 0.001               # 0.1% per side (~binanceus taker) — charged on entry and exit
STOP_LOSS = -0.27          # live bot's wide stop
ADX_MIN = 25
BUDGET = 300.0            # shared risk budget: V1 = 3x$100, V2 = corr-adj $ cap
COOLDOWN_H = 24 * 7        # halt a week after a breaker trip, then resume
CORR_REFRESH_H = 24 * 7    # recompute correlation weekly (as the live guardian would)
CORR_WINDOW_H = 24 * 90    # trailing 90d for correlation
VOL_WINDOW_D = 30          # trailing 30d for sizing vol
REG_RECENT_D = 14          # adaptive breaker: recent BTC vol window
REG_BASE_D = 365           # adaptive breaker: baseline BTC vol window

HOURS_PER_YEAR = 24 * 365


# ----------------------------------------------------------- load + signals ----

def load() -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(glob.glob(os.path.join(DATA_DIR, "*_USDT-1h.feather"))):
        coin = os.path.basename(f).split("_")[0]
        df = pd.read_feather(f)[["date", "open", "high", "low", "close"]].copy()
        df = df.set_index("date").astype(float)
        out[coin] = df
    return out


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].values
    df["ema20"] = talib.EMA(c, 20)
    df["ema50"] = talib.EMA(c, 50)
    df["ema100"] = talib.EMA(c, 100)
    df["adx"] = talib.ADX(df["high"].values, df["low"].values, c, 14)
    stacked = (df["close"] > df["ema20"]) & (df["ema20"] > df["ema50"]) & \
              (df["ema50"] > df["ema100"])
    df["entry"] = stacked & (df["adx"] > ADX_MIN)
    df["exit"] = df["close"] < df["ema50"]
    # trailing 30d daily vol, shifted 1 day so a bar can't see its own day
    daily = df["close"].resample("1D").last()
    dvol = daily.pct_change().rolling(VOL_WINDOW_D).std().shift(1)
    df["dvol"] = dvol.reindex(df.index, method="ffill")
    return df


def btc_regime_series(btc: pd.DataFrame) -> pd.Series:
    """Point-in-time recent/baseline BTC daily-vol ratio, hourly, no lookahead."""
    daily = btc["close"].resample("1D").last().pct_change()
    recent = daily.rolling(REG_RECENT_D).std().shift(1)
    base = daily.rolling(REG_BASE_D).std().shift(1)
    ratio = (recent / base).replace([np.inf, -np.inf], np.nan)
    return ratio.reindex(btc.index, method="ffill")


# ------------------------------------------------------------- risk overlay ----

def corr_adj_exposure(stakes: dict[str, float], C: pd.DataFrame) -> float:
    """sqrt(w^T C w) — correlated stakes count near their full sum; independent
    ones count less. Missing pairs default to corr 1.0 (conservative)."""
    coins = [c for c, v in stakes.items() if v]
    if not coins:
        return 0.0
    w = np.array([stakes[c] for c in coins])
    M = np.ones((len(coins), len(coins)))
    for i, a in enumerate(coins):
        for j, b in enumerate(coins):
            if not C.empty and a in C.index and b in C.columns and not np.isnan(C.loc[a, b]):
                M[i, j] = C.loc[a, b]
    return float(np.sqrt(max(float(w @ np.clip(M, -1, 1) @ w), 0.0)))


def vol_stake(dvol: float, wallet: float, target_pct=0.003,
              floor=40.0, cap=180.0) -> float:
    if not dvol or np.isnan(dvol) or dvol <= 0:
        return (floor + cap) / 2
    return float(min(max((target_pct * wallet) / dvol, floor), cap))


# ----------------------------------------------------------------- simulate ----

def simulate(data: dict[str, pd.DataFrame], regime: pd.Series, cfg: dict) -> dict:
    """cfg = {sizing: 'flat'|'vol', cap: 'count'|'corr', breaker: 'none'|'fixed'|'adaptive'}.
    Toggling one field at a time isolates each lever's contribution."""
    sizing, cap, breaker = cfg["sizing"], cfg["cap"], cfg["breaker"]
    coins = list(data)
    idx = data[coins[0]].index
    # aligned numpy views for speed
    close = {c: data[c]["close"].values for c in coins}
    entry = {c: data[c]["entry"].values for c in coins}
    exit_ = {c: data[c]["exit"].values for c in coins}
    dvol = {c: data[c]["dvol"].values for c in coins}
    rets = pd.DataFrame({c: data[c]["close"] for c in coins}).pct_change()
    reg = regime.values
    need_corr = (cap == "corr")

    cash = START_CASH
    pos: dict[str, dict] = {}          # coin -> {entry, stake, amount}
    peak = START_CASH
    halted_until = -1
    trips = 0
    n_trades = 0
    equity_curve = np.empty(len(idx))
    C = pd.DataFrame()

    for i in range(len(idx)):
        # mark to market
        equity = cash + sum(p["amount"] * close[c][i] for c, p in pos.items())
        equity_curve[i] = equity
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0

        # refresh correlation weekly (point-in-time trailing window)
        if need_corr and i % CORR_REFRESH_H == 0 and i > 200:
            win = rets.iloc[max(0, i - CORR_WINDOW_H):i].dropna()
            C = win.corr() if len(win) > 50 else C

        # RULE 0: circuit breaker (skip entirely when breaker=='none')
        if breaker != "none":
            r = reg[i]
            r = r if r and not np.isnan(r) else 1.0
            if breaker == "fixed":
                thr = 0.10
            elif breaker == "adaptive":                 # tighten-or-loosen (0.08–0.20)
                thr = min(max(0.10 * r, 0.08), 0.20)
            else:                                        # adaptive_loose: never below 0.10
                thr = min(max(0.10 * max(r, 1.0), 0.10), 0.20)
            if dd >= thr and pos and i >= halted_until:
                for c, p in list(pos.items()):
                    cash += p["amount"] * close[c][i] * (1 - FEE)
                pos.clear()
                trips += 1
                halted_until = i + COOLDOWN_H
                continue
            if i < halted_until:
                continue

        # exits (signal or hard stop)
        for c in list(pos):
            p = pos[c]
            pnl = close[c][i] / p["entry"] - 1
            if exit_[c][i] or pnl <= STOP_LOSS:
                cash += p["amount"] * close[c][i] * (1 - FEE)
                del pos[c]
                n_trades += 1

        # entries (deterministic coin order)
        for c in coins:
            if c in pos or not entry[c][i]:
                continue
            stake = 100.0 if sizing == "flat" else vol_stake(dvol[c][i], equity)
            stake = min(stake, cash)
            if stake < 20:
                continue
            if cap == "count":
                if len(pos) >= 3:            # count cap
                    continue
            else:
                proposed = {k: v["stake"] for k, v in pos.items()}
                proposed[c] = stake
                if corr_adj_exposure(proposed, C) > BUDGET:   # correlation-adjusted cap
                    continue
            price = close[c][i]
            pos[c] = {"entry": price, "stake": stake, "amount": stake / price}
            cash -= stake * (1 + FEE)

    eq = pd.Series(equity_curve, index=idx)
    roll_peak = eq.cummax()
    max_dd = float(((roll_peak - eq) / roll_peak).max())
    hourly_ret = eq.pct_change().dropna()
    ann_vol = float(hourly_ret.std() * np.sqrt(HOURS_PER_YEAR))
    total_ret = float(eq.iloc[-1] / START_CASH - 1)
    years = len(idx) / HOURS_PER_YEAR
    cagr = float((eq.iloc[-1] / START_CASH) ** (1 / years) - 1)
    return {
        "final": float(eq.iloc[-1]), "total_ret": total_ret,
        "cagr": cagr, "max_dd": max_dd, "ann_vol": ann_vol,
        "calmar": cagr / max_dd if max_dd > 0 else float("nan"),
        "trades": n_trades, "trips": trips, "equity": eq,
    }


# --------------------------------------------------------------------- main ----

def main():
    print("Loading data + computing indicators...")
    data = load()
    for c in data:
        data[c] = add_signals(data[c])
    regime = btc_regime_series(data["BTC"])

    # buy & hold BTC benchmark
    btc = data["BTC"]["close"]
    bh = btc / btc.iloc[0] * START_CASH
    bh_dd = float(((bh.cummax() - bh) / bh.cummax()).max())

    # ablation ladder: start from today's bot, switch ONE lever at a time
    ladder = [
        ("baseline: flat + count, NO breaker",  {"sizing": "flat", "cap": "count", "breaker": "none"}),
        ("+ vol sizing",                        {"sizing": "vol",  "cap": "count", "breaker": "none"}),
        ("+ correlation cap",                   {"sizing": "vol",  "cap": "corr",  "breaker": "none"}),
        ("+ fixed 10% breaker (today's rule)",  {"sizing": "vol",  "cap": "corr",  "breaker": "fixed"}),
        ("+ adaptive breaker (tighten+loosen)", {"sizing": "vol",  "cap": "corr",  "breaker": "adaptive"}),
        ("+ adaptive breaker (loosen-only) V2", {"sizing": "vol",  "cap": "corr",  "breaker": "adaptive_loose"}),
        ("TODAY'S BOT (flat+count+fixed)",      {"sizing": "flat", "cap": "count", "breaker": "fixed"}),
    ]
    results = []
    for label, cfg in ladder:
        print(f"Simulating: {label} ...")
        results.append((label, simulate(data, regime, cfg)))

    print("\n" + "=" * 92)
    print("RISK-BRAIN ABLATION  —  Jan 2021 → Jun 2026 (5.5y, 6 coins, $1000 start, long-only spot)")
    print("Each row switches ONE lever vs the row above, so the delta is that lever's isolated effect.")
    print("=" * 92)
    print(f"{'configuration':<38}{'return':>9}{'CAGR':>7}{'maxDD':>8}{'annVol':>8}"
          f"{'Calmar':>8}{'trades':>8}{'trips':>7}")
    print("-" * 92)
    for label, r in results:
        print(f"{label:<38}{r['total_ret']*100:>8.1f}%{r['cagr']*100:>6.1f}%"
              f"{r['max_dd']*100:>7.1f}%{r['ann_vol']*100:>7.1f}%"
              f"{r['calmar']:>8.2f}{r['trades']:>8d}{r['trips']:>7d}")
    print(f"{'buy & hold BTC':<38}{(bh.iloc[-1]/START_CASH-1)*100:>8.1f}%"
          f"{'':>6}{bh_dd*100:>7.1f}%")
    print("-" * 92)
    print("\nCalmar = CAGR / maxDD (higher = more return per unit of pain). "
          "This is the number that matters for a 'never blows up' brain.")

    best = dict(results)["+ fixed 10% breaker (today's rule)"]   # vol+corr+dormant breaker
    today = dict(results)["TODAY'S BOT (flat+count+fixed)"]
    print(f"\nRECOMMENDED (vol sizing + correlation cap + dormant fixed breaker) vs TODAY'S BOT:")
    print(f"  return  {today['total_ret']*100:+.1f}%  ->  {best['total_ret']*100:+.1f}%")
    print(f"  max DD  {today['max_dd']*100:.1f}%   ->  {best['max_dd']*100:.1f}%")
    print(f"  Calmar  {today['calmar']:.2f}    ->  {best['calmar']:.2f}  "
          f"({'BETTER' if best['calmar'] > today['calmar'] else 'WORSE'} risk-adjusted return)")

    out = pd.DataFrame({label: r["equity"] for label, r in results})
    out["bh_btc"] = bh
    out.to_csv("/Users/vikasreddy/cryptobot/risk_backtest_equity.csv")
    print("\nEquity curves -> risk_backtest_equity.csv")


if __name__ == "__main__":
    main()
