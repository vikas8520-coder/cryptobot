#!/usr/bin/env python3
"""Export daily OHLCV from yfinance to the Backtesting-Engine CSV format.

Format the engine's parseCsv expects: header
  timestamp,open,high,low,close,volume
with ISO-8601 timestamps, chronological. Only timestamp+close are strictly
required; we include all five for completeness.

Usage:
  python3 export_yf.py BTC-USD 3y
  python3 export_yf.py NIFTYBEES.NS 5y
Writes <TICKER>_<PERIOD>.csv in the current dir.
"""
import sys
import csv
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance not installed: pip install yfinance")


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
    period = sys.argv[2] if len(sys.argv) > 2 else "3y"
    out = f"{ticker.replace('-', '_')}_{period}.csv"

    print(f"fetching {ticker} daily, period={period} ...")
    df = yf.download(ticker, period=period, interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or len(df) == 0:
        sys.exit(f"no data for {ticker}")

    # yfinance multi-index columns -> flatten
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df = df.droplevel(1, axis=1)

    rows = []
    for ts, row in df.iterrows():
        # ensure tz-aware UTC ISO string like the engine produces
        t = ts
        if getattr(t, "tzinfo", None) is None:
            t = t.tz_localize("UTC")
        iso = t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({
            "timestamp": iso,
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        })

    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out}: {len(rows)} candles, "
          f"{rows[0]['timestamp'][:10]} -> {rows[-1]['timestamp'][:10]}")


if __name__ == "__main__":
    main()
