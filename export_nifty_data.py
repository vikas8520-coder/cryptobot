#!/usr/bin/env python3
"""Export NIFTYBEES.NS raw OHLCV to data/ for offline backtesting.

One-shot. Writes CSVs with a Date column + OHLCV so the backtest harness
never touches the network. Daily = long history (2009-) for the 200-DMA
brake + walk-forward; hourly = 1y for the live-style SMA(10/30) cross.
"""
import os
import yfinance as yf

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "data")
os.makedirs(OUT, exist_ok=True)


def pull(tkr, period, interval, fname):
    df = yf.Ticker(tkr).history(period=period, interval=interval)
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "Date"
    path = os.path.join(OUT, fname)
    df.to_csv(path)
    print(f"{fname}: {len(df)} rows {df.index[0].date()}..{df.index[-1].date()} -> {path}")


if __name__ == "__main__":
    pull("NIFTYBEES.NS", "max", "1d", "NIFTYBEES_daily.csv")
    pull("NIFTYBEES.NS", "1y", "1h", "NIFTYBEES_hourly.csv")
    pull("^NSEI", "max", "1d", "NSEI_index_daily.csv")
