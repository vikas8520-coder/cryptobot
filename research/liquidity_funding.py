"""Binance perp liquidity + regime-conditional funding for the RSI(2) coin screen.

WHY not the ticker API: Binance REST returns 451 here, so dollar volume is reconstructed from
the archived klines (base volume x close, summed per UTC day).

WHY regime-conditional funding: SolRSI2_1h only holds longs while the 4h trend filter is on, and
holds ~3h on average. A coin's all-sample mean funding therefore overstates what the strategy
actually pays -- what matters is funding during the hours the filter would have had us long.
"""

from pathlib import Path

import pandas as pd
import talib.abstract as ta

DATA = Path("/Users/vikasreddy/cryptobot/user_data/data/binance/futures")
START, END = "2022-01-01", "2026-06-30"
RECENT = "2026-01-01"          # "is it liquid now", not "was it liquid in 2021"


def rows(base: str) -> dict:
    d = pd.read_feather(DATA / f"{base}_USDT_USDT-1h-futures.feather")
    d = d[(d.date >= START) & (d.date <= END)]
    dv = (d.volume * d.close).groupby(d.date.dt.date).sum()
    rec = (d[d.date >= RECENT].volume * d[d.date >= RECENT].close)
    rec = rec.groupby(d[d.date >= RECENT].date.dt.date).sum()

    out = {"coin": base,
           "vol_med_musd": round(dv.median() / 1e6, 1),
           "vol_2026_musd": round(rec.median() / 1e6, 1) if len(rec) else None}

    # funding while the 4h uptrend filter is on
    f = DATA / f"{base}_USDT_USDT-1h-funding_rate.feather"
    inf4 = DATA / f"{base}_USDT_USDT-4h-futures.feather"
    if f.exists() and inf4.exists():
        fr = pd.read_feather(f)
        fr = fr[(fr.date >= START) & (fr.date <= END)]
        inf = pd.read_feather(inf4)
        inf["ema50"] = ta.EMA(inf, timeperiod=50)
        inf["ema200"] = ta.EMA(inf, timeperiod=200)
        inf["up"] = (inf.ema50 > inf.ema200) & (inf.close > inf.ema200)
        inf["date"] = (inf["date"] + pd.Timedelta(hours=4)).astype("datetime64[ms, UTC]")
        j = pd.merge_asof(fr.sort_values("date"), inf[["date", "up"]].sort_values("date"),
                          on="date", direction="backward")
        out["fund_all_apy"] = round(float(fr["open"].mean() * 3 * 365 * 100), 2)
        up = j[j.up.fillna(False)]
        out["fund_up_apy"] = round(float(up["open"].mean() * 3 * 365 * 100), 2) if len(up) else None
        out["pct_time_up"] = round(100 * float(inf["up"].mean()), 1)
    return out


if __name__ == "__main__":
    bases = sorted({f.name.split("_USDT")[0] for f in DATA.glob("*-1h-futures.feather")})
    df = pd.DataFrame([rows(b) for b in bases]).sort_values("vol_2026_musd", ascending=False)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False))
    df.to_csv("/Users/vikasreddy/cryptobot/research/liquidity_funding.csv", index=False)
