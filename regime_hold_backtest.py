#!/usr/bin/env python3
"""
regime_hold_backtest.py — test the "hold, but with a brake" thesis.

The lever the data pointed at: stop out-trading the market. HOLD it (capture the
big upside), but use a slow regime filter to step aside during the worst crashes —
cutting buy&hold's brutal drawdown while keeping most of the return. ~2 trades/yr,
so fees are irrelevant (the problem that killed the high-turnover strategies).

Compares, on DAILY data, Jan 2021 -> Jun 2026:
  - Buy & hold (benchmark)
  - Hold only while close > SMA(N)  [the moving-average brake], N in {100,150,200}
  - Hold only while close > EMA(50) [faster brake]
  - Vol-targeted hold (scale exposure by inverse recent vol, no on/off)
For BTC alone and for an equal-weight basket of all 6 coins.

No lookahead: signal from close[t], position applied to return[t+1] (shift 1).
Run: ./.venv/bin/python regime_hold_backtest.py
"""
from __future__ import annotations
import glob
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "user_data", "data", "binance_vision")
FEE = 0.001          # charged whenever exposure changes (switch cost)
DAYS_PER_YEAR = 365


def daily_closes() -> pd.DataFrame:
    cols = {}
    for f in sorted(glob.glob(os.path.join(DATA_DIR, "*_USDT-1h.feather"))):
        coin = os.path.basename(f).split("_")[0]
        s = pd.read_feather(f)[["date", "close"]].set_index("date")["close"].astype(float)
        cols[coin] = s.resample("1D").last()
    return pd.DataFrame(cols).dropna()


def metrics(equity: pd.Series, switches: int, time_in_mkt: float) -> dict:
    eq = equity.dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1
    years = len(eq) / DAYS_PER_YEAR
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    dd = ((eq.cummax() - eq) / eq.cummax()).max()
    dret = eq.pct_change().dropna()
    vol = dret.std() * np.sqrt(DAYS_PER_YEAR)
    return {"total": total, "cagr": cagr, "maxdd": dd,
            "calmar": cagr / dd if dd > 0 else float("nan"),
            "vol": vol, "switches": switches, "tim": time_in_mkt}


def run_position(price: pd.Series, pos: pd.Series) -> tuple[pd.Series, int, float]:
    """Given a price series and a target exposure series (0..1), return the
    equity curve net of switch fees, the switch count, and % time in market."""
    pos = pos.shift(1).fillna(0.0).clip(0, 1)          # act next day (no lookahead)
    ret = price.pct_change().fillna(0.0)
    strat = pos * ret
    turn = pos.diff().abs().fillna(pos)                 # exposure change each day
    strat = strat - turn * FEE                          # fee on the changed fraction
    equity = (1 + strat).cumprod()
    switches = int((pos.diff().abs() > 0.01).sum())
    tim = float((pos > 0).mean())
    return equity, switches, tim


def basket(closes: pd.DataFrame) -> pd.Series:
    """Equal-weight daily-rebalanced basket price index of all coins."""
    rets = closes.pct_change().fillna(0.0)
    idx = (1 + rets.mean(axis=1)).cumprod()
    return idx


def evaluate(name: str, price: pd.Series) -> list[tuple[str, dict]]:
    out = []
    # buy & hold
    eq, sw, tim = run_position(price, pd.Series(1.0, index=price.index))
    out.append((f"{name}: buy & hold", metrics(eq, sw, tim)))
    # moving-average brakes
    for n in (100, 150, 200):
        sig = (price > price.rolling(n).mean()).astype(float)
        eq, sw, tim = run_position(price, sig)
        out.append((f"{name}: hold > SMA{n}", metrics(eq, sw, tim)))
    # faster EMA brake
    ema50 = price.ewm(span=50, adjust=False).mean()
    sig = (price > ema50).astype(float)
    eq, sw, tim = run_position(price, sig)
    out.append((f"{name}: hold > EMA50", metrics(eq, sw, tim)))
    # vol-targeted hold (continuous exposure = target_vol / recent_vol, capped 1)
    dvol = price.pct_change().rolling(30).std()
    target = 0.03                                         # ~3% daily target
    expo = (target / dvol).clip(0, 1).fillna(0.0)
    eq, sw, tim = run_position(price, expo)
    out.append((f"{name}: vol-targeted hold", metrics(eq, sw, tim)))
    return out


def main():
    closes = daily_closes()
    print(f"Daily data: {closes.index.min().date()} -> {closes.index.max().date()} "
          f"({len(closes)} days, {list(closes.columns)})")

    rows = evaluate("BTC", closes["BTC"]) + evaluate("Basket", basket(closes))

    print("\n" + "=" * 96)
    print("HOLD-WITH-A-BRAKE  —  daily, 2021-2026, 0.1% switch fees")
    print("=" * 96)
    print(f"{'strategy':<28}{'return':>10}{'CAGR':>8}{'maxDD':>8}{'Calmar':>8}"
          f"{'annVol':>8}{'switch':>8}{'%inMkt':>8}")
    print("-" * 96)
    for name, m in rows:
        sep = name.startswith("Basket: buy")
        if sep:
            print("-" * 96)
        print(f"{name:<28}{m['total']*100:>9.0f}%{m['cagr']*100:>7.1f}%"
              f"{m['maxdd']*100:>7.0f}%{m['calmar']:>8.2f}{m['vol']*100:>7.0f}%"
              f"{m['switches']:>8d}{m['tim']*100:>7.0f}%")
    print("-" * 96)
    print("\nCalmar = CAGR/maxDD. The brake WINS if it lifts Calmar well above buy&hold's —")
    print("i.e. keeps most of the return while cutting the gut-wrenching drawdown.")


if __name__ == "__main__":
    main()
