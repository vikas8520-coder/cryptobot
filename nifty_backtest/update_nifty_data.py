#!/usr/bin/env python3
"""update_nifty_data.py

Extend the local Nifty 5m history from Yahoo Finance and the F&O BhavCopy cache.
Yahoo only provides the last ~60 days of 5m data, so this merges the old CSV
with a fresh download to add the most recent bars.
"""
import io
import os
import requests
import zipfile
from datetime import datetime, time as dtime

import pandas as pd
import yfinance as yf

CSV5 = "/Users/vikasreddy/cryptobot/data/NSEI_index_5m.csv"
CACHE = "/Users/vikasreddy/cryptobot/nifty_backtest/cache"


def download_5m():
    print("Downloading 5m ^NSEI from Yahoo Finance ...")
    df = yf.download("^NSEI", period="60d", interval="5m", progress=False)
    if df is None or df.empty:
        return None
    df = df.reset_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[1] == "" else c[0] for c in df.columns]
    df = df.rename(columns={"Datetime": "Date"})
    if df["Date"].dt.tz is None:
        df["Date"] = df["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    else:
        df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata")
    for c in ("Open", "High", "Low", "Close", "Volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
    return df


def merge_with_old(new_df):
    if not os.path.exists(CSV5):
        return new_df
    old = pd.read_csv(CSV5, index_col=0, parse_dates=True).reset_index()
    old = old.rename(columns={old.columns[0]: "Date"})
    old["Date"] = pd.to_datetime(old["Date"], errors="coerce").dt.tz_localize("Asia/Kolkata")
    # Keep old data only before the earliest Yahoo date
    earliest_new = new_df["Date"].min()
    old = old[old["Date"] < earliest_new]
    combined = pd.concat([old, new_df], ignore_index=True)
    combined = combined.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    return combined


def save_csv(df):
    df = df.sort_values("Date")
    df.to_csv(CSV5, index=False)
    print(f"Wrote {len(df)} rows to {CSV5} from {df['Date'].iloc[0]} to {df['Date'].iloc[-1]}")


def latest_bhavcopy_date():
    latest = None
    for fn in os.listdir(CACHE):
        if fn.startswith("BhavCopy_NSE_FO_") and fn.endswith("_F_0000.csv"):
            dstr = fn.split("_")[6]
            try:
                d = datetime.strptime(dstr, "%Y%m%d").date()
                if latest is None or d > latest:
                    latest = d
            except ValueError:
                pass
    return latest


def download_bhavcopy_fo(day):
    date_str = day.strftime("%Y%m%d")
    url = (
        f"https://archives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"
    )
    print(f"Downloading F&O BhavCopy for {day} ...")
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            print(f"  no bhavcopy for {day}: HTTP {r.status_code}")
            return False
        z = zipfile.ZipFile(io.BytesIO(r.content))
        csv_name = z.namelist()[0]
        out_path = os.path.join(CACHE, csv_name)
        with z.open(csv_name) as f:
            content = f.read()
        with open(out_path, "wb") as f:
            f.write(content)
        # keep the zip too
        zip_path = os.path.join(CACHE, os.path.basename(url))
        with open(zip_path, "wb") as f:
            f.write(r.content)
        print(f"  saved {out_path}")
        return True
    except Exception as e:
        print(f"  error downloading {day}: {e}")
        return False


def update_bhavcopies(latest_csv_date):
    new_df = download_5m()
    if new_df is None:
        return
    trading_days = sorted(new_df["Date"].dt.date.unique())
    if latest_csv_date is None:
        needed = trading_days
    else:
        needed = [d for d in trading_days if d > latest_csv_date]
    for d in needed:
        download_bhavcopy_fo(d)


def main():
    new_df = download_5m()
    if new_df is None:
        raise SystemExit("Failed to download 5m data")

    latest_cache = latest_bhavcopy_date()
    print(f"Latest BhavCopy in cache: {latest_cache}")
    update_bhavcopies(latest_cache)

    combined = merge_with_old(new_df)
    save_csv(combined)


if __name__ == "__main__":
    main()
