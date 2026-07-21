#!/usr/bin/env python3
"""
frontier_backtest.py — can we push the brake's return WITHOUT more drawdown?

Two principled levers the base brake backtest didn't model:

  LEVER 1 — yield on the cash sleeve. The brake is out of the market ~55% of the time;
            in reality that cash earns ~4-5% (T-bills / money-market / stable yield).
            Earned while FLAT, so it adds return with ZERO added drawdown.

  LEVER 2 — REAL diversification. The crypto "basket" isn't diversified (all coins crash
            together). Add assets with different risk drivers — gold (GLD), bonds (TLT),
            stocks (SPY) — each with its own 200-day brake. Diversification lowers combined
            drawdown, which RAISES Calmar (return per unit of pain). To convert that into
            more RETURN at the SAME drawdown budget you must size up (leverage) — shown last,
            with a borrow cost, so the tradeoff is honest.

No lookahead: signal from close[t], position applied to return[t+1] (run via shift).
Fees 0.1% on exposure changes. Macro sleeves forward-filled onto the crypto calendar
(markets closed on weekends = flat, no return).
"""
from __future__ import annotations
import warnings

import numpy as np
import pandas as pd

from regime_hold_backtest import daily_closes, basket, metrics, FEE, DAYS_PER_YEAR

warnings.filterwarnings("ignore")

CASH_YIELD = 0.045      # ~T-bill / stable-yield on the idle sleeve
BORROW = 0.07           # cost of leverage (annual) for the risk-budgeted line
MA = 200                # the brake lookback we run live


def braked_returns(price: pd.Series, cash_annual: float = 0.0, n: int = MA):
    """Daily strategy returns for a 200-day-braked hold of one asset, with the idle
    fraction earning cash_annual. Returns (strat_returns, position, in_market_frac)."""
    sma = price.rolling(n).mean()
    pos = (price > sma).astype(float).shift(1).fillna(0.0)      # act next day (no lookahead)
    ret = price.pct_change().fillna(0.0)
    cash_daily = (1 + cash_annual) ** (1 / DAYS_PER_YEAR) - 1
    strat = pos * ret + (1 - pos) * cash_daily                 # invested + idle-cash yield
    turn = pos.diff().abs().fillna(pos)
    strat = strat - turn * FEE                                 # fee on the changed fraction
    return strat, pos, float((pos > 0).mean())


def eq_metrics(strat: pd.Series, tim: float, switches: int = 0) -> dict:
    equity = (1 + strat).cumprod()
    return metrics(equity, switches, tim)


def macro_closes(index: pd.DatetimeIndex) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(["GLD", "TLT", "SPY"], start="2020-10-01", end="2026-07-01",
                     auto_adjust=True, progress=False)
    close = (df["Close"] if "Close" in df else df).copy()
    # normalize both sides to tz-naive ns-normalized dates so reindex can compare them
    close.index = pd.to_datetime(close.index, utc=True).tz_convert(None).normalize()
    tgt = pd.to_datetime(index).tz_localize(None).normalize() \
        if getattr(index, "tz", None) is not None else pd.to_datetime(index).normalize()
    close = close.reindex(tgt, method="ffill")                 # onto crypto's daily calendar
    close.index = index                                        # restore for downstream alignment
    return close


def main():
    closes = daily_closes()
    closes.index = pd.to_datetime(closes.index).normalize()    # clean ns-naive dates
    idx = closes.index
    crypto = basket(closes)                                    # equal-weight 6-coin index
    print(f"Crypto daily: {idx.min().date()} -> {idx.max().date()} ({len(idx)} days)")

    try:
        mac = macro_closes(idx)
        macro_ok = mac[["GLD", "TLT", "SPY"]].dropna().shape[0] > 500
    except Exception as e:
        print(f"macro fetch failed ({e}); running crypto-only")
        macro_ok = False

    rows = []

    # ---------- LEVER 1: cash yield on the idle sleeve ----------
    for label, px in (("BTC", closes["BTC"]), ("Crypto basket", crypto)):
        s0, _, tim = braked_returns(px, 0.0)
        s1, _, _ = braked_returns(px, CASH_YIELD)
        rows.append((f"{label}: brake, 0% cash", eq_metrics(s0, tim)))
        rows.append((f"{label}: brake + {CASH_YIELD*100:.1f}% cash", eq_metrics(s1, tim)))

    # ---------- LEVER 2: real cross-asset diversification ----------
    if macro_ok:
        sleeves = {"crypto": crypto, "gold": mac["GLD"], "bonds": mac["TLT"], "stocks": mac["SPY"]}
        sleeve_ret, tims = {}, {}
        for k, px in sleeves.items():
            s, _, tim = braked_returns(px, CASH_YIELD)
            sleeve_ret[k] = s
            tims[k] = tim
        R = pd.DataFrame(sleeve_ret).dropna()

        crypto_only = R["crypto"]
        cm = eq_metrics(crypto_only, tims["crypto"])
        rows.append(("— diversification (all braked + cash) —", None))
        rows.append(("crypto-only braked", cm))

        div = R.mean(axis=1)                                   # equal-weight 4 sleeves, daily rebal
        dm = eq_metrics(div, float(np.mean(list(tims.values()))))
        rows.append(("diversified 4-sleeve (equal wt)", dm))

        # risk-budgeted: lever the diversified sleeve up to crypto-only's drawdown
        target_dd = cm["maxdd"]
        k = max(1.0, target_dd / dm["maxdd"]) if dm["maxdd"] > 0 else 1.0
        borrow_daily = (k - 1) * BORROW / DAYS_PER_YEAR
        levered = k * div - borrow_daily
        lm = eq_metrics(levered, dm["tim"])
        rows.append((f"diversified, sized to {target_dd*100:.0f}% DD (×{k:.2f}, {BORROW*100:.0f}% borrow)", lm))

    # ---------- print ----------
    print("\n" + "=" * 92)
    print("PUSHING THE FRONTIER — daily 2021-2026, 0.1% fees, no lookahead")
    print("=" * 92)
    print(f"{'strategy':<44}{'return':>9}{'CAGR':>8}{'maxDD':>8}{'Calmar':>8}{'%inMkt':>8}")
    print("-" * 92)
    for name, m in rows:
        if m is None:
            print("-" * 92 + f"\n{name}")
            continue
        print(f"{name:<44}{m['total']*100:>8.0f}%{m['cagr']*100:>7.1f}%"
              f"{m['maxdd']*100:>7.0f}%{m['calmar']:>8.2f}{m['tim']*100:>7.0f}%")
    print("-" * 92)
    print("\nRead: Lever 1 (cash) should lift return with maxDD UNCHANGED. Lever 2 should lift")
    print("Calmar; the sized-to-DD line shows the return you can get at crypto-only's drawdown.")


if __name__ == "__main__":
    main()
