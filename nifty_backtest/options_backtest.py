#!/usr/bin/env python3
"""options_backtest.py -- the locked HA + EMA34 daily-bias 5m strategy's signals,
mapped onto real NSE UDiFF bhavcopy ATM CE/PE premiums instead of raw index
points, with realistic spread costs.

WHY this exists: the 5m index backtest (ha_ema34_5m_backtest.py) shows the raw
strategy's edge, but the user's real claim is 79% on NIFTY *options*. Options
add premium bleed (theta), IV crush, bid-ask, and expiry-day mechanics that the
index backtest ignores. This file maps every 5m trade (side = bias direction) to
the ATM option of the nearest expiry listed strictly after the entry date, using
actual NSE bhavcopy closes/highs/lows, and charges a per-side spread (default
Rs 7.5, sensitivity 0 / 7.5 / 15).

Execution model (matches the locked 5m harness):
- Signals at bar close, filled at the NEXT bar's close (no look-ahead); stops
  intrabar. The fill loop below is a BYTE-FOR-BYTE copy of dbt.backtest with
  per-trade fill prices/dates added -- parity with dbt.backtest is asserted
  (entry, exit, bars, ret), raise on mismatch, so the loop can't drift.
- Index entry/exit prices are NOT the P&L; they only decide WHEN a premium leg
  is bought/sold. Premium mapping (settled 2026-08-17):
  * entry premium = ATM option's daily CLOSE on the entry day (TtlTradgVol > 0
    required, else nearest liquid strike of the same expiry)
  * stop exit = option's own LOW (CE held) / HIGH (PE held) on the exit day
  * bias_flip / eod exit = CLS on the exit day
  * if the trade is still open when the held expiry E arrives (exit date >= E),
    force-close at E's CLS on E's own day and re-enter the next expiry ATM at
    that same close (each roll costs another round-trip spread), walking until
    the exit date is reached
- ATM = strike nearest NIFTY spot (UndrlygPric col 20); strikes ~50 apart near
  spot; lot 65 (NewBrdLotQty col 28).
- Spread: Rs 7.5/side default; long pays prem+s/2 at entry, receives prem-s/2
  at exit; short is the reverse. Per-lot P&L = premium diff * 65, reduced by
  Rs spread per leg.

Remaining honest caveats: no IV/theta model (premiums are the market's actual
mark), entry at daily CLOSE is optimistic for a 5m strategy, ~57 trading days is
a small sample, and expiry bleed means long-hold wins pay theta. The comparison
against the TradingView 79% claim is the point: this is the spread-, bleed- and
slippage-adjusted reality check.
"""

import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ha_ema34_daily_bias_backtest as dbt  # reuse proven logic (noqa: E402)

CSV5 = os.path.join(HERE, "..", "data", "NSEI_index_5m.csv")
CSVD = os.path.join(HERE, "..", "data", "NSEI_index_daily_recent.csv")
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "options_ha_ema34_5m_results.json")

EMA_SPAN = 34
LOT = 65
SPREADS = (0.0, 7.5, 15.0)  # Rs per side

BHAV_NAMES = ["TradDt", "BizDt", "FinInstrmTp", "TckrSymb", "SctySrs", "XpryDt",
              "FininstrmActlXpryDt", "StrkPric", "OptnTp", "FinInstrmNm",
              "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "NewBrdLotQty"]
BHAV_USE = [0, 1, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22,
            23, 24, 28]


def load_csv(path):
    """Our CSVs are index-first (datetime) -- normalize to a Date column."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = df.sort_index().reset_index()
    df = df.rename(columns={df.columns[0]: "Date"})
    return df


def load_bhavcopies(cache):
    """Load every bhavcopy in cache/ (filenames BhavCopy_NSE_FO_0_0_0_YYYYMMDD_...)
    into {date: NIFTY-IDO-only frame}. Column mapping verified against the raw
    header on 2026-08-17 (TckrSymb=7, XpryDt=9, StrkPric=11, OptnTp=12, prices
    14-17, UndrlygPric=20, TtlTradgVol=24, NewBrdLotQty=28)."""
    out = {}
    for fn in sorted(os.listdir(cache)):
        if not fn.startswith("BhavCopy_NSE_FO_") or not fn.endswith("_F_0000.csv"):
            continue
        dstr = fn.split("_")[6]
        try:
            d = pd.Timestamp(dstr)
        except ValueError:
            continue
        df = pd.read_csv(os.path.join(cache, fn), header=0, usecols=BHAV_USE,
                         names=BHAV_NAMES)
        df = df[(df["FinInstrmTp"] == "IDO") & (df["TckrSymb"] == "NIFTY")].copy()
        for c in ("StrkPric", "OpnPric", "HghPric", "LwPric", "ClsPric",
                  "UndrlygPric"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["TtlTradgVol"] = pd.to_numeric(df["TtlTradgVol"], errors="coerce").fillna(0)
        df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce").dt.normalize()
        out[d.normalize()] = df
    return out


def signals_5m(df5, dfd):
    """5m entries/exits -- verbatim copy of ha_ema34_5m_backtest.signals_5m:
    bias for a 5m bar on day D = HA bias of day D-1 (prev-day convention); EMA34
    on 5m High/Low; long_ent = bias & close > ema_high, short_ent = ~bias &
    close < ema_low; bias-flip exits are the UNshifted convention."""
    ho, hc, _, _ = dbt.ha_candles(dfd)
    bias_daily = pd.Series(hc > ho, index=dfd["Date"])
    bias = (bias_daily.shift(1)
            .reindex(df5["Date"].dt.normalize())
            .fillna(False)
            .reset_index(drop=True))
    ema_high = dbt.ema(df5["High"], EMA_SPAN)
    ema_low = dbt.ema(df5["Low"], EMA_SPAN)
    warm = np.arange(len(df5)) >= EMA_SPAN

    long_ent = (bias & (df5["Close"] > ema_high)).fillna(False)
    short_ent = ((~bias) & (df5["Close"] < ema_low)).fillna(False)
    long_exit = (~bias).values
    short_exit = bias.values
    for s in (long_ent, short_ent):
        s[~warm] = False
    return long_ent, short_ent, long_exit, short_exit


def backtest_with_prices(df, long_ent, short_ent, long_exit, short_exit,
                         fee_side, exit_on_bias_flip):
    """Byte-for-byte copy of dbt.backtest, plus per-trade fill prices/dates.
    Trades carry: entry, exit (date strings), entry_px, exit_px, bars, ret,
    reason. Parity vs dbt.backtest is asserted by the caller."""
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    dates = df["Date"]
    n = len(df)

    le = long_ent.values
    se = short_ent.values
    lx = long_exit
    sx = short_exit

    in_pos = 0  # +1 long, -1 short
    entry_px = 0.0
    entry_i = 0
    sl = 0.0
    equity = np.ones(n)
    eq = 1.0
    trades = []

    for i in range(1, n):
        px = close[i]
        if in_pos != 0:
            if in_pos == 1:
                sl = max(sl, low[i - 1])
            else:
                sl = min(sl, high[i - 1])
            fill = px
            reason = None
            stop_hit = (in_pos == 1 and low[i] <= sl) or (in_pos == -1 and high[i] >= sl)
            if stop_hit:
                fill = sl
                reason = "stop"
            elif exit_on_bias_flip and (
                    (in_pos == 1 and lx[i - 1]) or (in_pos == -1 and sx[i - 1])):
                fill = px
                reason = "bias_flip"
            if reason:
                gross = fill / entry_px if in_pos == 1 else 2.0 - fill / entry_px
                net = gross * (1 - fee_side) ** 2
                eq *= net
                trades.append({"entry": str(dates[entry_i]), "exit": str(dates[i]),
                               "entry_px": float(entry_px), "exit_px": float(fill),
                               "bars": i - entry_i, "ret": net - 1, "reason": reason,
                               "side": "long" if in_pos == 1 else "short"})
                in_pos = 0
                equity[i] = eq
            else:
                equity[i] = eq * (px / entry_px if in_pos == 1 else 2.0 - px / entry_px)
        else:
            equity[i] = eq
            if le[i - 1] or se[i - 1]:
                in_pos = 1 if le[i - 1] else -1
                entry_px = px
                entry_i = i
                sl = low[i - 1] if in_pos == 1 else high[i - 1]

    if in_pos != 0:
        fill = close[-1]
        gross = fill / entry_px if in_pos == 1 else 2.0 - fill / entry_px
        net = gross * (1 - fee_side) ** 2
        eq *= net
        trades.append({"entry": str(dates[entry_i]), "exit": str(dates.iloc[-1]),
                       "entry_px": float(entry_px), "exit_px": float(fill),
                       "bars": n - 1 - entry_i, "ret": net - 1, "reason": "eod",
                       "side": "long" if in_pos == 1 else "short"})
        equity[-1] = eq

    return pd.Series(equity, index=dates), trades


def assert_parity(trades, ref_trades):
    """The copy must not drift: same (entry, exit, bars, ret) as dbt.backtest."""
    key = lambda t: (t["entry"], t["exit"], t["bars"], round(t["ret"], 12))
    mine = [key(t) for t in trades]
    ref = [key(t) for t in ref_trades]
    if mine != ref:
        for i, (a, b) in enumerate(zip(mine, ref)):
            if a != b:
                raise AssertionError(f"parity mismatch at trade {i}: mine={a} ref={b}")
        raise AssertionError(f"trade count mismatch: mine={len(mine)} ref={len(ref)}")
    return True


def opt_row(frames, day, expiry, strike, opt_type):
    df = frames.get(day.normalize())
    if df is None:
        return None
    m = df[(df["XpryDt"] == expiry) & (df["StrkPric"] == strike) & (df["OptnTp"] == opt_type)]
    return m.iloc[0] if len(m) else None


def atm_strike(frames, day, expiry):
    """ATM = strike nearest UndrlygPric on day; vol-guard: first liquid strike of
    the same expiry scanning outward from spot (settled 2026-08-17)."""
    df = frames.get(day.normalize())
    if df is None:
        return None
    exp = df[df["XpryDt"] == expiry]
    if not len(exp):
        return None
    spot = exp["UndrlygPric"].dropna().iloc[0]
    exp = exp.assign(_d=(exp["StrkPric"] - spot).abs()).sort_values("_d")
    for _, r in exp.iterrows():
        if r["TtlTradgVol"] > 0:
            return r["StrkPric"]
    return exp.iloc[0]["StrkPric"]  # no liquid strike: fall back to nearest


def next_expiry(frames, day):
    """Smallest listed XpryDt strictly after day (from that day's own listing)."""
    df = frames.get(day.normalize())
    if df is None:
        return None
    exps = sorted(df["XpryDt"].dropna().unique())
    for e in exps:
        if e > day.normalize():
            return e
    return None


def map_trade(t, frames, spread):
    """Map one 5m index trade to premium legs. Returns dict with per-lot PnL,
    premium-% return, expiry, strike, and the legs that were traded."""
    entry_day = pd.Timestamp(t["entry"])
    exit_day = pd.Timestamp(t["exit"])
    opt_type = "CE" if t["side"] == "long" else "PE"
    expiry = next_expiry(frames, entry_day)
    if expiry is None:
        raise RuntimeError(f"no expiry after {t['entry']}")
    strike = atm_strike(frames, entry_day, expiry)
    legs = []  # (day, expiry, strike, opt_type, premium, side_of_cashflow)
    total_pnl = 0.0  # per lot, before spread

    def buy_prem(day, e, k):
        r = opt_row(frames, day, e, k, opt_type)
        if r is None:
            raise RuntimeError(f"missing {opt_type} {k}@{e.date()} on {day.date()}")
        return float(r["ClsPric"])

    # entry leg: CLS on entry day
    p0 = buy_prem(entry_day, expiry, strike)
    legs.append((entry_day, expiry, strike, opt_type, p0, "entry"))
    day, e, k = entry_day, expiry, strike
    last_prem = p0
    while e <= exit_day:  # held into expiry: force close at E, roll to next
        pE = buy_prem(e, e, k)
        legs.append((e, e, k, opt_type, pE, "expiry_close"))
        total_pnl += (pE - last_prem) if t["side"] == "long" else (last_prem - pE)
        day, e = e, next_expiry(frames, e)
        if e is None or e <= day:
            raise RuntimeError(f"no further expiry after {day.date()} (exit {t['exit']})")
        k = atm_strike(frames, day, e)
        pN = buy_prem(day, e, k)
        legs.append((day, e, k, opt_type, pN, "entry"))
        last_prem = pN
    # final exit on exit_day
    r = opt_row(frames, exit_day, e, k, opt_type)
    if r is None:
        raise RuntimeError(f"missing {opt_type} {k}@{e.date()} on {exit_day.date()}")
    if t["reason"] == "stop":
        pX = float(r["LwPric"] if opt_type == "CE" else r["HghPric"])
    else:
        pX = float(r["ClsPric"])
    legs.append((exit_day, e, k, opt_type, pX, "exit"))
    total_pnl += (pX - last_prem) if t["side"] == "long" else (last_prem - pX)

    n_legs = sum(1 for lg in legs if lg[5] in ("entry", "expiry_close", "exit"))
    pnl = (total_pnl - spread * n_legs) * LOT
    prem_ret = pnl / (p0 * LOT)
    return {"entry": t["entry"], "exit": t["exit"], "side": t["side"],
            "reason": t["reason"], "opt_type": opt_type, "expiry": str(expiry.date()),
            "strike": strike, "entry_prem": p0, "n_legs": n_legs,
            "pnl_rupees": round(pnl, 2), "prem_ret": round(prem_ret, 6),
            "ret": round(prem_ret, 6), "bars": t["bars"]}


def premium_curve(trades_mapped, df5):
    """Compound premium-% returns on the 5m bar axis (flat between closes)."""
    eq = np.ones(len(df5))
    by_exit = {}
    for m in trades_mapped:
        by_exit.setdefault(m["exit"], []).append(m)
    cur = 1.0
    for i, d in enumerate(df5["Date"]):
        for m in by_exit.get(str(d), []):
            cur *= 1.0 + m["prem_ret"]
        eq[i] = cur
    return pd.Series(eq, index=df5["Date"])


def main():
    df5 = load_csv(CSV5)
    dfd = load_csv(CSVD)
    frames = load_bhavcopies(CACHE)
    if not frames:
        raise SystemExit("no bhavcopies loaded")

    le, se, lx, sx = signals_5m(df5, dfd)

    # ---- parity gate: the price-copy must match dbt.backtest exactly ----
    mine_eq, trades = backtest_with_prices(df5, le, se, lx, sx, 0.0, True)
    _, ref_trades = dbt.backtest(df5, le, se, lx, sx, 0.0, True)
    assert_parity(trades, ref_trades)
    print(f"parity OK: {len(trades)} trades (expect 757)")

    # ---- map every trade to ATM premium legs, at each spread ----
    mapped = [map_trade(t, frames, 0.0) for t in trades]
    if len(mapped) != len(trades):
        raise SystemExit("mapping lost trades")

    years = (df5["Date"].iloc[-1] - df5["Date"].iloc[0]).days / 365.25
    results = []
    for spread in SPREADS:
        ms = [map_trade(t, frames, spread) for t in trades]
        eq = premium_curve(ms, df5)
        m = dbt.metrics(eq, ms, years, len(df5))
        cum_rupee = np.cumsum([x["pnl_rupees"] for x in ms])
        peak = np.maximum.accumulate(np.maximum.accumulate(cum_rupee))
        dd_r = float((cum_rupee - peak).min())
        wins = [x for x in ms if x["pnl_rupees"] > 0]
        m.update({
            "variant": f"ATM options bias-flip (spread Rs {spread}/side)",
            "spread_per_side_rs": spread,
            "total_rupees_per_lot": round(float(cum_rupee[-1]), 2),
            "win_rate_rupee_pct": round(100 * len(wins) / len(ms), 1),
            "avg_pnl_rupees": round(float(np.mean([x["pnl_rupees"] for x in ms])), 2),
            "max_dd_rupees_per_lot": round(dd_r, 2),
            "n_expiry_rolls": sum(1 for x in ms if x["n_legs"] > 2),
        })
        results.append(m)

    cols = ["variant", "total_return_pct", "max_drawdown_pct", "trades",
            "win_rate_pct", "profit_factor", "sharpe", "avg_hold_bars",
            "total_rupees_per_lot", "win_rate_rupee_pct", "avg_pnl_rupees",
            "max_dd_rupees_per_lot", "n_expiry_rolls"]
    pd.set_option("display.width", 260)
    print(pd.DataFrame(results)[cols].to_string(index=False))

    with open(OUT, "w") as f:
        json.dump({
            "data_5m": CSV5, "data_daily": CSVD, "cache": CACHE, "ema_span": EMA_SPAN,
            "lot": LOT, "spreads_rs": list(SPREADS),
            "note": "5m HA+EMA34 signals mapped to NSE bhavcopy ATM CE/PE premiums; "
                    "entry = ATM CLS on entry day (vol>0), stop = option LOW/HIGH, "
                    "bias_flip/eod = CLS; roll at expiry CLS; spread Rs/side; "
                    "premium-% equity compresses leverage -- rupee totals are the "
                    "real P&L per lot (65). No IV/theta model; entry at daily CLOSE "
                    "is optimistic for a 5m strategy.",
            "results": results,
            "trades": mapped}, f, indent=2)
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
