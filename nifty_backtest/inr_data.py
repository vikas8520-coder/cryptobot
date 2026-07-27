"""inr_data.py -- Indian-exchange BTC/INR data adapters, extracted from
btc_faber_backtest.py so the India-tax CLI (backtest_india_tax.py) and the
Faber harness share ONE fetch/cache path instead of two copies drifting apart.

FREEZE-SAFE: every fetcher caches its raw API response to cache/ on first
successful call, then reads the cache on every subsequent call -- offline runs
(tests, frozen backtests) never touch the network once warmed.

Fallback chain (WazirX is defunct, CoinDCX geo-blocks non-Indian IPs):
  fetch_wazirx_inr -> (caller falls back to) fetch_coindcx_inr -> fetch_coingecko_inr
fetch_coindcx_inr falls back to fetch_coingecko_inr internally on any error.
"""
import json
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_coingecko_inr(coin_id="bitcoin", vs_currency="inr", days=365, cache=True):
    """Fetch daily OHLCV for BTC/INR from CoinGecko (aggregates Indian exchanges:
    CoinDCX, ZebPay, etc.). Caches to cache/ so freeze-safe runs need no network.

    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    cache_file = os.path.join(CACHE_DIR, f"{coin_id}_{vs_currency}_{days}d.json")
    if cache and os.path.exists(cache_file):
        with open(cache_file) as f:
            data = json.load(f)
    else:
        import urllib.request
        url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
               f"?vs_currency={vs_currency}&days={days}&interval=daily")
        req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if cache:
            with open(cache_file, "w") as f:
                json.dump(data, f)

    # CoinGecko returns [timestamp, price] arrays
    prices = data.get("prices", [])
    total_vols = data.get("total_volumes", [])
    # CoinGecko market_chart doesn't give OHLC; derive from prices (daily close)
    # For a proper OHLC we'd need the /coins/{id}/ohlc endpoint
    rows = []
    for i, (ts, px) in enumerate(prices):
        vol = total_vols[i][1] if i < len(total_vols) else 0.0
        rows.append({
            "Date": pd.Timestamp(ts, unit="ms").normalize(),
            "Open": px, "High": px, "Low": px, "Close": px, "Volume": vol,
        })
    df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    return df


def fetch_coingecko_ohlc_inr(coin_id="bitcoin", vs_currency="inr", days=365, cache=True):
    """Fetch proper OHLC daily data from CoinGecko /ohlc endpoint (INR).
    Returns DataFrame with Date, Open, High, Low, Close.
    """
    cache_file = os.path.join(CACHE_DIR, f"{coin_id}_{vs_currency}_{days}d_ohlc.json")
    if cache and os.path.exists(cache_file):
        with open(cache_file) as f:
            data = json.load(f)
    else:
        import urllib.request
        url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
               f"?vs_currency={vs_currency}&days={days}")
        req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if cache:
            with open(cache_file, "w") as f:
                json.dump(data, f)

    # Each entry: [timestamp, open, high, low, close]
    rows = []
    for ts, o, h, l, c in data:
        rows.append({
            "Date": pd.Timestamp(ts, unit="ms").normalize(),
            "Open": o, "High": h, "Low": l, "Close": c, "Volume": 0.0,
        })
    df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    return df


def fetch_wazirx_inr(symbol="BTCINR", interval="1d", limit=365, cache=True):
    """Fetch daily OHLCV from WazirX API (historical klines endpoint).
    Note: WazirX exchange is defunct (post-FTX collapse) but the API endpoint
    still serves historical data. Returns DataFrame with Date, Open, High, Low, Close, Volume.

    WazirX kline format: [timestamp, open, high, low, close, volume]
    """
    cache_file = os.path.join(CACHE_DIR, f"wazirx_{symbol}_{interval}_{limit}d.json")
    if cache and os.path.exists(cache_file):
        with open(cache_file) as f:
            data = json.load(f)
    else:
        import urllib.request
        url = (f"https://api.wazirx.com/sapi/v1/klines"
               f"?symbol={symbol}&interval={interval}&limit={limit}")
        req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if cache:
            with open(cache_file, "w") as f:
                json.dump(data, f)

    rows = []
    for ts, o, h, l, c, v in data:
        # WazirX returns timestamps in SECONDS, not milliseconds
        rows.append({
            "Date": pd.Timestamp(int(ts), unit="s").normalize(),
            "Open": float(o), "High": float(h), "Low": float(l),
            "Close": float(c), "Volume": float(v),
        })
    df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    return df


def fetch_coindcx_inr(pair="BTCUSDT", interval="1D", limit=365, cache=True):
    """Fetch daily OHLCV from CoinDCX API (Indian exchange, now geo-blocked for US IPs).
    Requires no auth for public market data endpoints. Returns DataFrame with
    Date, Open, High, Low, Close, Volume.

    CoinDCX candles format: [timestamp, open, high, low, close, volume]
    Falls back to CoinGecko if CoinDCX is unreachable (SSL/geo issues).
    """
    cache_file = os.path.join(CACHE_DIR, f"coindcx_{pair}_{interval}_{limit}d.json")
    if cache and os.path.exists(cache_file):
        with open(cache_file) as f:
            data = json.load(f)
    else:
        import urllib.request
        url = (f"https://api.coindcx.io/api/v2/market/candles"
               f"?pair={pair}&interval={interval}&limit={limit}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cryptobot-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            if cache and data:
                with open(cache_file, "w") as f:
                    json.dump(data, f)
        except Exception:
            # CoinDCX often unreachable from non-Indian IPs; fall back to CoinGecko
            return fetch_coingecko_inr(days=limit)

    rows = []
    for ts, o, h, l, c, v in data:
        rows.append({
            "Date": pd.Timestamp(ts, unit="ms").normalize(),
            "Open": float(o), "High": float(h), "Low": float(l),
            "Close": float(c), "Volume": float(v),
        })
    df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    return df
