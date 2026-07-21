#!/usr/bin/env python3
"""
risk_lib.py — the shared risk math for the "risk brain".

Pure, dependency-light functions used by BOTH the live Portfolio Guardian and
the strategies' position sizing, plus the backtest harness. No Freqtrade import,
so it can be unit-tested and reasoned about in isolation.

Three ideas live here:
  1. CORRELATION-AWARE EXPOSURE  — treat a correlated cluster of positions as the
     single fat bet it really is (crypto "diversification" is mostly fake).
  2. VOLATILITY-SCALED SIZING    — risk a constant $ of expected pain per trade,
     so a calm coin gets a bigger position than a wild one.
  3. ADAPTIVE DRAWDOWN LIMIT     — widen the circuit breaker when the market is
     genuinely volatile, tighten it when calm, so it fires on real danger not chop.

Data source: local 1h feathers in user_data/data/binance_vision (refresh with
`freqtrade download-data`). For live use the numbers are trailing-window estimates;
they drift slowly, so daily-ish freshness is fine.
"""
from __future__ import annotations
import glob
import os
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")  # silence pyarrow feather deprecation noise

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "user_data", "data", "binance_vision")
HOURS_PER_DAY = 24
DAYS_PER_YEAR = 365

# ------------------------------------------------------------------ data ----

_CLOSE_CACHE: dict[str, pd.Series] = {}


def coin_of(pair: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC'. Robust to spot and futures pair strings."""
    return pair.split("/")[0].upper()


def _load_close(coin: str) -> pd.Series | None:
    coin = coin.upper()
    if coin in _CLOSE_CACHE:
        return _CLOSE_CACHE[coin]
    path = os.path.join(DATA_DIR, f"{coin}_USDT-1h.feather")
    if not os.path.exists(path):
        _CLOSE_CACHE[coin] = None
        return None
    df = pd.read_feather(path)[["date", "close"]].set_index("date")
    s = df["close"].astype(float)
    _CLOSE_CACHE[coin] = s
    return s


def available_coins() -> list[str]:
    return sorted(os.path.basename(f).split("_")[0]
                  for f in glob.glob(os.path.join(DATA_DIR, "*_USDT-1h.feather")))


def hourly_returns(coins: list[str], lookback_days: int | None = None) -> pd.DataFrame:
    """Aligned hourly % returns for the given coins over the trailing window."""
    cols = {}
    for c in coins:
        s = _load_close(c)
        if s is not None:
            cols[c.upper()] = s
    if not cols:
        return pd.DataFrame()
    px = pd.DataFrame(cols).dropna()
    if lookback_days is not None:
        px = px.tail(lookback_days * HOURS_PER_DAY)
    return px.pct_change().dropna()


# --------------------------------------------- 1. correlation-aware caps ----

def corr_matrix(coins: list[str], lookback_days: int = 90) -> pd.DataFrame:
    """Correlation of hourly returns over the trailing window."""
    rets = hourly_returns(coins, lookback_days)
    if rets.empty:
        return pd.DataFrame()
    return rets.corr()


def corr_adjusted_exposure(stakes: dict[str, float], lookback_days: int = 90) -> float:
    """
    Effective (correlation-adjusted) $ exposure of a set of positions.

        E_eff = sqrt( w^T C w )

    where w is the vector of per-coin $ stakes and C their return-correlation
    matrix. Intuition:
      * all correlated (C=1)  -> E_eff = sum(stakes)   (fully concentrated: no credit)
      * uncorrelated (C=I)    -> E_eff = sqrt(sum w^2) (much smaller: real diversification)
    So capping E_eff instead of raw sum(stakes) automatically punishes fake
    crypto diversification and rewards genuinely independent bets.

    Unknown coins (no data) are treated as perfectly correlated to everything —
    the conservative assumption for a risk cap.
    """
    stakes = {coin_of(k): float(v) for k, v in stakes.items() if v}
    if not stakes:
        return 0.0
    coins = list(stakes)
    w = np.array([stakes[c] for c in coins])
    C = corr_matrix(coins, lookback_days)
    # build a matrix aligned to `coins`; default corr 1.0 (conservative) where missing
    M = np.ones((len(coins), len(coins)))
    for i, a in enumerate(coins):
        for j, b in enumerate(coins):
            if not C.empty and a in C.index and b in C.columns and not np.isnan(C.loc[a, b]):
                M[i, j] = C.loc[a, b]
    M = np.clip(M, -1.0, 1.0)
    var = float(w @ M @ w)
    return float(np.sqrt(max(var, 0.0)))


def diversification_ratio(stakes: dict[str, float], lookback_days: int = 90) -> float:
    """sum(stakes)/E_eff. 1.0 = no diversification; >1 = correlation gives credit."""
    raw = sum(abs(v) for v in stakes.values())
    eff = corr_adjusted_exposure(stakes, lookback_days)
    return raw / eff if eff > 0 else 1.0


# ------------------------------------------- 2. volatility-scaled sizing ----

def daily_vol(coin: str, lookback_days: int = 30) -> float | None:
    """Realized daily volatility (std of daily returns) over the trailing window.
    e.g. 0.05 == the coin typically moves ~5% a day."""
    s = _load_close(coin)
    if s is None:
        return None
    daily = s.resample("1D").last().dropna() if hasattr(s.index, "freq") or True else s
    d = daily.tail(lookback_days + 1).pct_change().dropna()
    if len(d) < 5:
        return None
    v = float(d.std())
    return v if v > 0 else None


def vol_scaled_stake(coin: str, wallet: float, target_risk_pct: float = 0.003,
                     floor: float = 40.0, cap: float = 180.0,
                     lookback_days: int = 30) -> float:
    """
    $ stake so that a typical 1-day move risks ~target_risk_pct of the wallet.

        stake = (target_risk_pct * wallet) / daily_vol(coin)

    A calm coin (small daily_vol) earns a bigger stake; a wild coin a smaller one,
    so every position contributes roughly equal risk. Clamped to [floor, cap] so a
    freakishly calm reading can't produce an absurd position. Falls back to the
    midpoint if the coin has no data.

    Default target 0.3%/wallet per typical daily move centres a ~2.8%-vol coin near
    a $100 stake on a $1000 wallet (our old flat baseline), then spreads calm coins
    up and wild coins down from there.
    """
    v = daily_vol(coin, lookback_days)
    if v is None:
        return float(min(max((floor + cap) / 2, floor), cap))
    raw = (target_risk_pct * wallet) / v
    return float(min(max(raw, floor), cap))


# --------------------------------------------- 3. adaptive drawdown limit ----

def btc_vol_regime(lookback_days: int = 14, baseline_days: int = 365) -> float:
    """Ratio of recent BTC daily vol to its longer-run baseline.
    ~1.0 = normal, >1 = market unusually volatile, <1 = unusually calm."""
    recent = daily_vol("BTC", lookback_days)
    base = daily_vol("BTC", baseline_days)
    if not recent or not base:
        return 1.0
    return recent / base


def adaptive_dd_limit(base: float = 0.10, floor: float = 0.08, ceil: float = 0.20,
                      lookback_days: int = 14) -> float:
    """
    Circuit-breaker threshold that flexes with market volatility.

    In calm markets a 10% portfolio drop is a genuine alarm; in a violent market
    it may be normal noise, and a fixed breaker would sell the bottom. We scale the
    threshold by the current BTC vol regime and clamp to [floor, ceil].
    """
    regime = btc_vol_regime(lookback_days=lookback_days)
    return float(min(max(base * regime, floor), ceil))


# ----------------------------------------------------------------- demo ----

if __name__ == "__main__":
    coins = available_coins()
    print("Coins with data:", coins)
    print("\n--- 1. Correlation-aware exposure ---")
    demo = {"BTC": 100, "ETH": 100, "SOL": 100}
    eff = corr_adjusted_exposure(demo, lookback_days=90)
    print(f"3x $100 in BTC/ETH/SOL: raw exposure $300, "
          f"correlation-adjusted ${eff:.0f} "
          f"(div ratio {diversification_ratio(demo):.2f}x)")
    demo2 = {"BTC": 150, "XRP": 150}
    print(f"$150 BTC + $150 XRP (less correlated): raw $300, "
          f"adjusted ${corr_adjusted_exposure(demo2):.0f} "
          f"(div ratio {diversification_ratio(demo2):.2f}x)")

    print("\n--- 2. Volatility-scaled sizing (wallet $1000, old flat stake was $100) ---")
    for c in coins:
        v = daily_vol(c, 30)
        st = vol_scaled_stake(c, 1000)
        print(f"  {c:4s} daily vol {v*100:4.1f}%  ->  stake ${st:5.0f}"
              if v else f"  {c:4s} (no vol)  ->  stake ${st:5.0f}")

    print("\n--- 3. Adaptive drawdown limit ---")
    print(f"  BTC vol regime (recent/baseline): {btc_vol_regime():.2f}x")
    print(f"  Adaptive breaker threshold: {adaptive_dd_limit()*100:.1f}% "
          f"(fixed was 10.0%)")
