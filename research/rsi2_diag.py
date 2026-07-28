"""Per-coin diagnostics for the SolRSI2_1h edge -- the "why", independent of the backtester.

WHY: a backtest gives one P&L number per coin, which conflates the signal's edge with the
ROI/stop plumbing. This measures the edge directly, so a coin that fails can be diagnosed as
"no reversion" vs "reversion too small to clear fees" vs "signal too rare".

For each coin, on the SolRSI2_1h entry condition (RSI(2) < 9 AND close > EMA50(1h) AND
4h EMA50 > EMA200 AND 4h close > EMA200):
  vol1h   median |1h return|, in %          -- must clear 0.10% round-trip taker
  n       signal count over the window
  fwd1/4/12  mean forward return after signal, %   (the reversion itself)
  edge12  fwd12 minus the unconditional mean 12h return  -- reversion net of drift
  hit2    P(+2% ROI target hit before -4% stop), the actual race the strategy runs
  t       t-stat of fwd12 vs 0 (overlapping samples, so read as a rough guide only)
  fund    mean funding, annualised %        -- a long-only strategy pays this
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import talib.abstract as ta

DATA = Path("/Users/vikasreddy/cryptobot/user_data/data/binance/futures")
START, END = "2022-01-01", "2026-06-30"
RSI_BUY, ROI, STOP, MAXBARS = 9, 0.02, -0.04, 12


def load(base: str) -> pd.DataFrame | None:
    f1, f4 = DATA / f"{base}_USDT_USDT-1h-futures.feather", DATA / f"{base}_USDT_USDT-4h-futures.feather"
    if not (f1.exists() and f4.exists()):
        return None
    d = pd.read_feather(f1)
    inf = pd.read_feather(f4)
    d["rsi2"] = ta.RSI(d, timeperiod=2)
    d["ema50"] = ta.EMA(d, timeperiod=50)
    inf["ema50"] = ta.EMA(inf, timeperiod=50)
    inf["ema200"] = ta.EMA(inf, timeperiod=200)
    # replicate merge_informative_pair: 4h values are only known at candle close, so shift
    # forward one 4h bar before the as-of join -- otherwise the filter peeks.
    inf = inf[["date", "close", "ema50", "ema200"]].copy()
    inf["date"] = (inf["date"] + pd.Timedelta(hours=4)).astype("datetime64[ms, UTC]")
    d = pd.merge_asof(d.sort_values("date"), inf.sort_values("date").add_suffix("_4h"),
                      left_on="date", right_on="date_4h", direction="backward")
    return d


def race(highs: np.ndarray, lows: np.ndarray, entry: float) -> int | None:
    """Did +2% ROI or the -4% stop come first, within MAXBARS? 1 / 0 / None if neither."""
    for h, lo in zip(highs, lows):
        if lo <= entry * (1 + STOP):
            return 0
        if h >= entry * (1 + ROI):
            return 1
    return None


def funding_apy(base: str) -> float | None:
    f = DATA / f"{base}_USDT_USDT-1h-funding_rate.feather"
    if not f.exists():
        return None
    d = pd.read_feather(f)
    d = d[(d.date >= START) & (d.date <= END)]
    return float(d["open"].mean() * 3 * 365 * 100) if len(d) else None


def diag(base: str) -> dict | None:
    d = load(base)
    if d is None:
        return None
    d = d[(d.date >= START) & (d.date <= END)].reset_index(drop=True)
    if len(d) < 24 * 365 * 2:                       # need 2y+ to say anything
        return {"coin": base, "n": -1, "years": len(d) / 24 / 365}
    c = d["close"].to_numpy()
    ret1 = np.abs(np.diff(c) / c[:-1]) * 100

    sig = ((d.rsi2 < RSI_BUY) & (d.close > d.ema50)
           & (d.ema50_4h > d.ema200_4h) & (d.close_4h > d.ema200_4h)).to_numpy().copy()
    sig[-MAXBARS - 1:] = False                       # no room to measure the outcome
    idx = np.flatnonzero(sig)

    def fwd(h):
        return (c[idx + h] / c[idx] - 1) * 100 if len(idx) else np.array([])

    f1, f4, f12 = fwd(1), fwd(4), fwd(12)
    base12 = (c[12:] / c[:-12] - 1) * 100            # unconditional 12h drift
    hi, lo_ = d["high"].to_numpy(), d["low"].to_numpy()
    races = [race(hi[i + 1:i + 1 + MAXBARS], lo_[i + 1:i + 1 + MAXBARS], c[i]) for i in idx]
    dec = [r for r in races if r is not None]

    return {
        "coin": base,
        "years": round(len(d) / 24 / 365, 1),
        "vol1h": round(float(np.median(ret1)), 3),
        "n": len(idx),
        "fwd1": round(float(f1.mean()), 3) if len(idx) else None,
        "fwd4": round(float(f4.mean()), 3) if len(idx) else None,
        "fwd12": round(float(f12.mean()), 3) if len(idx) else None,
        "edge12": round(float(f12.mean() - base12.mean()), 3) if len(idx) else None,
        "t": round(float(f12.mean() / (f12.std(ddof=1) / np.sqrt(len(f12)))), 2) if len(idx) > 5 else None,
        "hit2": round(100 * np.mean(dec), 1) if dec else None,
        "undec": len(races) - len(dec),
        "fund": round(funding_apy(base), 2) if funding_apy(base) is not None else None,
    }


if __name__ == "__main__":
    bases = sys.argv[1:] or sorted({f.name.split("_")[0] for f in DATA.glob("*-1h-futures.feather")})
    rows = [r for b in bases if (r := diag(b))]
    df = pd.DataFrame(rows).sort_values("edge12", ascending=False, na_position="last")
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))
    df.to_csv("/Users/vikasreddy/cryptobot/research/rsi2_diag.csv", index=False)
