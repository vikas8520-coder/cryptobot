#!/usr/bin/env python
"""Download CoinDCX history into freqtrade's feather layout.

WHY this bypasses `freqtrade download-data` (audit 2026-07-28): freqtrade's
downloader would ask the exchange for a 4h timeframe and for funding-rate /
mark-price history. CoinDCX serves none of those — 4h is not a valid interval
and there is no funding history endpoint at all — so the standard path either
errors or writes empty files. This script writes all four artefacts freqtrade's
futures backtester expects, and is explicit about which are real:

  <PAIR>-1h-futures.feather       REAL   spot OHLCV (see caveat below)
  <PAIR>-4h-futures.feather       REAL   resampled from the 1h series
  <PAIR>-1h-mark.feather          PROXY  copy of close; no mark history exists
  <PAIR>-1h-funding_rate.feather  ZERO   no funding history exists

CAVEAT worth repeating at the point of use: /market_data/candles serves the
SPOT book, not the perp. CoinDCX's INR-M perps are `ecode` "B" (Binance-
mirrored) and track spot closely, but basis and funding are NOT in this data.
Backtests off it are therefore funding-free and basis-free — optimistic by
roughly the funding carry of the holding period.

Writing zeros for funding is a deliberate choice over omitting the file:
freqtrade treats a missing funding file as a hard load error, and fabricating a
plausible-looking rate would silently bias PnL. Zero is wrong but visibly wrong.

Usage:
    .venv/bin/python scripts/download_coindcx_data.py                 # default 3 pairs, from 2022-01-01
    .venv/bin/python scripts/download_coindcx_data.py --pairs SOL/USDT:USDT --since 2023-01-01
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "user_data"))
import coindcx_api as api  # noqa: E402

DEFAULT_PAIRS = ["SOL/USDT:USDT", "LINK/USDT:USDT", "ADA/USDT:USDT"]
DEFAULT_SINCE = "2022-01-01"
DATADIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "user_data", "data", "coindcx", "futures"
)


def ft_filename(symbol, timeframe, kind="futures"):
    """'SOL/USDT:USDT' -> 'SOL_USDT_USDT-1h-futures.feather' (freqtrade's scheme)."""
    stem = symbol.replace("/", "_").replace(":", "_")
    return f"{stem}-{timeframe}-{kind}.feather"


def to_frame(rows):
    """ccxt rows -> freqtrade's dataframe: tz-aware `date` + OHLCV floats."""
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)
    return df.astype({c: "float64" for c in ("open", "high", "low", "close", "volume")})


def write(df, path):
    # reset_index because freqtrade's feather loader assumes a clean RangeIndex.
    df.reset_index(drop=True).to_feather(path, compression="lz4")
    return len(df)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", nargs="+", default=DEFAULT_PAIRS)
    p.add_argument("--since", default=DEFAULT_SINCE)
    p.add_argument("--datadir", default=DATADIR)
    p.add_argument("--timeframes", nargs="+", default=["1h", "4h"])
    args = p.parse_args()

    os.makedirs(args.datadir, exist_ok=True)
    since_ms = int(datetime.strptime(args.since, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)

    for symbol in args.pairs:
        pair = api.ft_to_dcx(symbol)
        print(f"\n=== {symbol}  ({pair}) ===", flush=True)

        def progress(_p, _tf, cursor, n):
            ts = datetime.fromtimestamp(cursor / 1000, timezone.utc).date()
            print(f"    ...{ts} ({n} candles)", end="\r", flush=True)

        rows = api.candles_range(pair, "1h", since_ms, progress=progress)
        if not rows:
            print(f"  !! no data for {pair} — skipped")
            continue

        base = to_frame(rows)
        span = f"{base['date'].iloc[0]:%Y-%m-%d} -> {base['date'].iloc[-1]:%Y-%m-%d}"
        print(f"  1h: {len(base)} candles  {span}          ")

        # Gaps are worth surfacing: a silent hole becomes a fake price jump that
        # an RSI-2 mean-reversion strategy will happily "trade".
        gaps = base["date"].diff().dt.total_seconds().div(3600).sub(1).gt(0).sum()
        if gaps:
            missing = int(base["date"].diff().dt.total_seconds().div(3600).sub(1).clip(lower=0).sum())
            print(f"  !! {gaps} gap(s), {missing} missing 1h candles")

        for tf in args.timeframes:
            data = rows if tf == "1h" else api.resample(rows, "1h", tf)
            if not data:
                print(f"  !! {tf}: nothing to write")
                continue
            df = to_frame(data)
            n = write(df, os.path.join(args.datadir, ft_filename(symbol, tf)))
            print(f"  {tf}: wrote {n} candles")

        # Mark proxy: OHLC of the traded price. Freqtrade only reads `close` off
        # this for liquidation-price math, which dry-run never exercises.
        write(base, os.path.join(args.datadir, ft_filename(symbol, "1h", "mark")))

        # Funding: 8h stamps at 00/08/16 UTC, all zero. Same schema freqtrade
        # writes for real exchanges (rate in `open`, other columns zero).
        fund = base[base["date"].dt.hour.isin((0, 8, 16))][["date"]].copy()
        for c in ("open", "high", "low", "close", "volume"):
            fund[c] = 0.0
        write(fund, os.path.join(args.datadir, ft_filename(symbol, "1h", "funding_rate")))
        print(f"  mark: {len(base)} rows (close proxy) | funding: {len(fund)} rows (ZERO)")

    print(f"\nDone -> {os.path.abspath(args.datadir)}")


if __name__ == "__main__":
    main()
