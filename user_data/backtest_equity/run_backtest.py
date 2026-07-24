#!/usr/bin/env python3
"""Headless port of Miles-Deutscher/Backtesting-Engine/src/backtest.ts.

Faithful re-implementation of runBacktest() so Vikas can get numbers from the
terminal without the browser UI. Same algorithm -> same metrics as the engine.

Usage:
  python3 run_backtest.py <csv> [strategy] [fast] [slow]
    strategy: sma-crossover | buy-and-hold   (default sma-crossover)
    fast/slow: SMA periods for crossover      (default 1 / 200 = 200-day brake)

Run the 200-day brake on BTC-USD 3y:
  python3 export_yf.py BTC-USD 3y
  python3 run_backtest.py BTC-USD_3y.csv sma-crossover 1 200
  python3 run_backtest.py BTC-USD_3y.csv buy-and-hold
"""
import sys
import csv

STARTING_CASH = 10000.0
FEE_PERCENT = 0.1  # engine default feePercent


def load_candles(path):
    import math
    candles = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("close"):
                continue
            try:
                cl = float(r["close"])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(cl):   # drop NaN/inf rows (yfinance gaps)
                continue
            candles.append({
                "timestamp": r["timestamp"].strip(),
                "open": float(r.get("open") or r["close"]),
                "high": float(r.get("high") or r["close"]),
                "low": float(r.get("low") or r["close"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume") or 0),
            })
    candles.sort(key=lambda c: c["timestamp"])
    if len(candles) < 2:
        sys.exit("CSV needs at least two valid rows")
    return candles


def _avg(xs):
    return sum(xs) / len(xs)


def run_backtest(candles, strategy, fast, slow, buf=0.0):
    if strategy == "sma-crossover" and fast >= slow:
        raise ValueError("fast must be < slow")
    cash = STARTING_CASH
    units = 0.0
    entry_value = 0.0
    fee = FEE_PERCENT / 100.0
    trades = []
    equity = []
    bars_in_market = 0
    prev_fast = None
    prev_slow = None

    for i, c in enumerate(candles):
        should_buy = (strategy == "buy-and-hold" and i == 0)
        should_sell = False

        if strategy == "sma-crossover" and i >= slow - 1:
            fast_ma = _avg([x["close"] for x in candles[i - fast + 1:i + 1]])
            slow_ma = _avg([x["close"] for x in candles[i - slow + 1:i + 1]])
            if prev_fast is not None and prev_slow is not None:
                if prev_fast <= prev_slow and fast_ma > slow_ma:
                    should_buy = True
                elif prev_fast >= prev_slow and fast_ma < slow_ma:
                    should_sell = True
            prev_fast, prev_slow = fast_ma, slow_ma

        elif strategy == "brake-buffer" and i >= slow - 1:
            # 200-day brake with hysteresis band: enter only when price is BUF%
            # ABOVE the 200d line, exit only when BUF% BELOW. buf=0 == hard cross.
            sma200 = _avg([x["close"] for x in candles[i - slow + 1:i + 1]])
            if c["close"] > sma200 * (1 + buf):
                should_buy = True
            elif c["close"] < sma200 * (1 - buf):
                should_sell = True

        if should_buy and units == 0:
            units = (cash * (1 - fee)) / c["close"]
            entry_value = cash
            cash = 0.0
            trades.append({"timestamp": c["timestamp"], "side": "Buy", "price": c["close"]})

        if should_sell and units > 0:
            cash = units * c["close"] * (1 - fee)
            trades.append({"timestamp": c["timestamp"], "side": "Sell", "price": c["close"],
                           "returnPercent": (cash / entry_value - 1) * 100,
                           "profitLoss": cash - entry_value})
            units = 0.0

        if units > 0:
            bars_in_market += 1
        equity.append({"date": c["timestamp"], "value": cash + units * c["close"]})

    if units > 0:
        last = candles[-1]
        cash = units * last["close"] * (1 - fee)
        trades.append({"timestamp": last["timestamp"], "side": "Sell", "price": last["close"],
                       "returnPercent": (cash / entry_value - 1) * 100,
                       "profitLoss": cash - entry_value})
        equity[-1] = {"date": last["timestamp"], "value": cash}

    peak = STARTING_CASH
    max_dd = 0.0
    for p in equity:
        peak = max(peak, p["value"])
        max_dd = max(max_dd, (peak - p["value"]) / peak)

    closed = [t for t in trades if t["side"] == "Sell"]
    winners = sum(1 for t in closed if (t.get("returnPercent") or 0) > 0)
    gross_profit = sum(max(t.get("profitLoss") or 0, 0) for t in closed)
    gross_loss = sum(abs(min(t.get("profitLoss") or 0, 0)) for t in closed)
    eq_vals = [p["value"] for p in equity]

    return {
        "strategy": strategy,
        "netReturn": (cash / STARTING_CASH - 1) * 100,
        "netProfit": cash - STARTING_CASH,
        "maxDrawdown": max_dd * 100,
        "winRate": (winners / len(closed) * 100) if closed else None,
        "profitFactor": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "closedTradeCount": len(closed),
        "averageTradeReturn": (_avg([t.get("returnPercent") or 0 for t in closed])) if closed else None,
        "peakEquity": max(eq_vals),
        "lowestEquity": min(eq_vals),
        "startingCash": STARTING_CASH,
        "timeInMarket": (bars_in_market / len(candles)) * 100,
        "endingCash": cash,
    }


def _fmt(r):
    pf = "n/a" if r["profitFactor"] is None else f"{r['profitFactor']:.2f}"
    wr = "n/a" if r["winRate"] is None else f"{r['winRate']:.1f}%"
    at = "n/a" if r["averageTradeReturn"] is None else f"{r['averageTradeReturn']:.2f}%"
    return (f"  strategy        : {r['strategy']}\n"
            f"  net return      : {r['netReturn']:.2f}%\n"
            f"  net profit      : ${r['netProfit']:.2f}\n"
            f"  max drawdown    : {r['maxDrawdown']:.2f}%\n"
            f"  profit factor   : {pf}\n"
            f"  win rate        : {wr}\n"
            f"  avg trade return: {at}\n"
            f"  closed trades   : {r['closedTradeCount']}\n"
            f"  time in market  : {r['timeInMarket']:.1f}%\n"
            f"  ending cash     : ${r['endingCash']:.2f}")


if __name__ == "__main__":
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD_3y.csv"
    strategy = sys.argv[2] if len(sys.argv) > 2 else "sma-crossover"
    fast = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    slow = int(sys.argv[4]) if len(sys.argv) > 4 else 200
    buf = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
    candles = load_candles(csv_path)
    print(f"=== {csv_path}  ({len(candles)} candles) ===")
    res = run_backtest(candles, strategy, fast, slow, buf)
    print(_fmt(res))
