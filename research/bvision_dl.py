"""Bulk-fetch Binance USDT-M perp history from data.binance.vision into freqtrade feathers.

WHY: the live Binance REST API returns 451 from this machine (geo-restricted), but the
public data.binance.vision archive is not gated. Since the India deployment plan targets
Binance (audit 2026-07-28), the RSI(2) coin-screen must be measured on Binance perp data,
not an OKX proxy.

Writes, per symbol, exactly what freqtrade's futures backtester reads:
  user_data/data/binance/futures/<PAIR>-1h-futures.feather   (klines)
  user_data/data/binance/futures/<PAIR>-4h-futures.feather   (resampled from 1h, UTC-aligned)
  user_data/data/binance/futures/<PAIR>-1h-mark.feather      (markPriceKlines)
  user_data/data/binance/futures/<PAIR>-1h-funding_rate.feather (8h-spaced events, 1h filename
      per freqtrade's _ft_has default -- see exchange.py:159)

Missing months are 404s (pre-listing) and are skipped, so listing date falls out of the data.
"""

import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly"
OUT = Path("/Users/vikasreddy/cryptobot/user_data/data/binance/futures")
START, END = "2021-12", "2026-06"          # monthly archives only; current month is partial
WORKERS = 16

SYMBOLS = """
BTC ETH BNB XRP ADA SOL DOGE TRX LTC BCH ETC XLM ATOM NEAR DOT AVAX ALGO HBAR ICP APT
SUI SEI TIA TON STX EGLD LINK UNI AAVE CRV LDO INJ DYDX GRT FIL IMX RUNE ONDO ENA WLD
RENDER TAO POL MATIC SHIB PEPE BONK ORDI GALA SAND MANA CHZ
""".split()

KCOLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
FCOLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]

sess = requests.Session()


def months(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m") for d in pd.date_range(f"{start}-01", f"{end}-01", freq="MS")]


def grab(url: str) -> pd.DataFrame | None:
    """Fetch one monthly zip -> DataFrame, or None if absent/unreadable."""
    try:
        r = sess.get(url, timeout=60)
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = z.namelist()[0]
            with z.open(name) as fh:
                head = fh.readline()
                fh.seek(0)
                # some archives carry a header row, some don't
                skip = 1 if head[:1] in (b"o", b"c") else 0
                return pd.read_csv(fh, header=None, skiprows=skip)
    except Exception:
        return None


def ohlcv(sym: str, kind: str) -> pd.DataFrame | None:
    """kind: 'klines' or 'markPriceKlines'. `sym` is the full market symbol, e.g. AVAXUSDT."""
    parts = []
    for m in months(START, END):
        df = grab(f"{BASE}/{kind}/{sym}/1h/{sym}-1h-{m}.zip")
        if df is not None and len(df):
            parts.append(df)
    if not parts:
        return None
    d = pd.concat(parts, ignore_index=True)
    d.columns = KCOLS[:d.shape[1]]
    # binance switched open_time from ms to us in some 2025+ archives
    unit = "us" if d["open_time"].max() > 1e14 else "ms"
    out = pd.DataFrame({
        "date": pd.to_datetime(d["open_time"], unit=unit, utc=True).astype("datetime64[ms, UTC]"),
        "open": d["open"].astype(float), "high": d["high"].astype(float),
        "low": d["low"].astype(float), "close": d["close"].astype(float),
        "volume": d["volume"].astype(float),
    })
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def funding(sym: str) -> pd.DataFrame | None:
    parts = []
    for m in months(START, END):
        df = grab(f"{BASE}/fundingRate/{sym}/{sym}-fundingRate-{m}.zip")
        if df is not None and len(df):
            parts.append(df)
    if not parts:
        return None
    d = pd.concat(parts, ignore_index=True)
    d.columns = FCOLS[:d.shape[1]]
    unit = "us" if d["calc_time"].max() > 1e14 else "ms"
    out = pd.DataFrame({
        "date": pd.to_datetime(d["calc_time"], unit=unit, utc=True).astype("datetime64[ms, UTC]"),
        "open": d["last_funding_rate"].astype(float),
    })
    for c in ("high", "low", "close", "volume"):
        out[c] = 0.0
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def resample_4h(d: pd.DataFrame) -> pd.DataFrame:
    r = d.set_index("date").resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return r.dropna(subset=["open"]).reset_index()


def one(base: str) -> str:
    sym, pair = f"{base}USDT", f"{base}_USDT_USDT"
    k = ohlcv(sym, "klines")
    if k is None or len(k) < 24 * 30:
        return f"{base:8s} SKIP (no/short kline history)"
    k.to_feather(OUT / f"{pair}-1h-futures.feather")
    resample_4h(k).to_feather(OUT / f"{pair}-4h-futures.feather")
    m = ohlcv(sym, "markPriceKlines")
    if m is not None:
        m.to_feather(OUT / f"{pair}-1h-mark.feather")
    f = funding(sym)
    if f is not None:
        f.to_feather(OUT / f"{pair}-1h-funding_rate.feather")
    return (f"{base:8s} {len(k):6d} 1h candles  {k.date.min():%Y-%m-%d} -> {k.date.max():%Y-%m-%d}"
            f"  mark={0 if m is None else len(m)} fund={0 if f is None else len(f)}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    syms = sys.argv[1:] or SYMBOLS
    with ThreadPoolExecutor(WORKERS) as ex:
        for line in ex.map(one, syms):
            print(line, flush=True)
