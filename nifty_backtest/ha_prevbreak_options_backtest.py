#!/usr/bin/env python3
"""ha_prevbreak_options_backtest.py

Backtest the friend's Heikin-Ashi "previous high/low break" rule on 1m or 5m
NIFTY index data, mapped to actual NSE weekly option premiums around Rs 180.

Rules:
- Compute Heikin-Ashi candles.
- Entry at bar i when HA close breaks above previous HA high  -> long CE.
- Entry at bar i when HA close breaks below previous HA low   -> long PE.
- Only one trade at a time; the stronger breakout wins if both fire.
- Option: nearest listed expiry, strike whose closing premium for the side (CE/PE)
  is closest to Rs 180, preferring liquid strikes.
- Target = +5% of entry premium; stop = -5% of entry premium.
- Intraday target/stop are estimated via index points using delta=0.5 for a
  premium-180 near-ATM option.  EOD exits use the option's actual closing premium.
- Per-side spread is subtracted for both entry and exit.

Honest caveats:
- Entry premium is the option's daily close, not the premium at the exact 1m/5m
  signal time.  Delta=0.5 is an approximation; real delta varies by moneyness and IV.
- If both target and stop are hit inside the same 1m/5m bar, the result depends on
  which occurred first.  Two variants are reported (target-first and stop-first).
- 1m history from Yahoo is only ~7-8 trading days; 5m history is ~57 trading days.
"""

import argparse
import json
import os
import sys
from datetime import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CSV5 = os.path.join(HERE, "..", "data", "NSEI_index_5m.csv")
CSV1 = os.path.join(HERE, "..", "data", "NSEI_index_1m.csv")
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "ha_prevbreak_options_results.json")

TARGET_PCT = 0.05
STOP_PCT = 0.05
DELTA = 0.50
LOT = 65
SPREADS = [0.0, 1.0, 2.0, 3.0]  # per side, rupees
MIN_VOLUME = 1

BHAV_NAMES = ["TradDt", "BizDt", "FinInstrmTp", "TckrSymb", "SctySrs", "XpryDt",
              "FininstrmActlXpryDt", "StrkPric", "OptnTp", "FinInstrmNm",
              "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric",
              "PrvsClsgPric", "UndrlygPric", "OpnIntrst", "ChngInOpnIntrst",
              "TtlTradgVol", "NewBrdLotQty"]
BHAV_USE = [0, 1, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22,
            23, 24, 28]


def load_csv(path):
    """Our index CSVs have Datetime as the first column or index."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = df.sort_index().reset_index()
    df = df.rename(columns={df.columns[0]: "Date"})
    for c in ("Open", "High", "Low", "Close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    return df


def load_bhavcopies(cache):
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
                  "UndrlygPric", "TtlTradgVol", "NewBrdLotQty"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["TtlTradgVol"] = pd.to_numeric(df["TtlTradgVol"], errors="coerce").fillna(0)
        df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce").dt.normalize()
        out[d.normalize()] = df
    return out


def ha_candles(df):
    """Standard Heikin-Ashi from OHLC."""
    o = df["Open"].values
    h = df["High"].values
    l = df["Low"].values
    c = df["Close"].values
    n = len(df)
    ho = np.empty(n)
    hc = np.empty(n)
    hh = np.empty(n)
    hl = np.empty(n)
    ho[0] = o[0]
    for i in range(n):
        hc[i] = (o[i] + h[i] + l[i] + c[i]) / 4.0
        if i > 0:
            ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
        hh[i] = max(h[i], ho[i], hc[i])
        hl[i] = min(l[i], ho[i], hc[i])
    return ho, hc, hh, hl


def next_expiry(frames, day):
    df = frames.get(day.normalize())
    if df is None:
        return None
    exps = sorted(df["XpryDt"].dropna().unique())
    for e in exps:
        if e > day.normalize():
            return e
    return None


def select_option(frames, day, opt_type, target_premium=180.0):
    """Pick the nearest expiry and the strike whose ClsPric is closest to
    target_premium for the chosen option type, preferring liquid strikes."""
    df = frames.get(day.normalize())
    if df is None:
        return None
    expiry = next_expiry(frames, day)
    if expiry is None:
        return None
    m = df[(df["XpryDt"] == expiry) & (df["OptnTp"] == opt_type)].copy()
    m = m[(m["ClsPric"] > 0)]
    if m.empty:
        return None
    liquid = m[m["TtlTradgVol"] >= MIN_VOLUME]
    if not liquid.empty:
        m = liquid
    m["_dist"] = (m["ClsPric"] - target_premium).abs()
    m = m.sort_values(["_dist", "TtlTradgVol"], ascending=[True, False])
    if m.empty:
        return None
    row = m.iloc[0]
    return {
        "expiry": expiry,
        "strike": float(row["StrkPric"]),
        "premium": float(row["ClsPric"]),
        "lot": int(row["NewBrdLotQty"]) if pd.notna(row["NewBrdLotQty"]) else LOT,
        "volume": int(row["TtlTradgVol"]),
        "spot": float(row["UndrlygPric"]) if pd.notna(row["UndrlygPric"]) else None,
    }


def opt_row(frames, day, expiry, strike, opt_type):
    df = frames.get(day.normalize())
    if df is None:
        return None
    m = df[(df["XpryDt"] == expiry) & (df["StrkPric"] == strike) & (df["OptnTp"] == opt_type)]
    return m.iloc[0] if len(m) else None


def backtest(df, frames, spread_per_side=0.0, same_bar="target", entry_mode="close"):
    """Run one backtest for a given per-side spread and same-bar assumption."""
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    open_ = df["Open"].values
    dates = df["Date"]
    n = len(df)

    ho, hc, hh, hl = ha_candles(df)

    in_pos = None
    entry_i = 0
    p0 = 0.0
    strike = None
    expiry = None
    opt_lot = LOT
    entry_index = 0.0
    target_index = 0.0
    stop_index = 0.0
    opt_type = None

    trades = []
    equity = 1.0
    equity_curve = [1.0]

    def close_trade(exit_i, reason, p_exit):
        nonlocal in_pos, equity
        pnl_per = p_exit - p0 - 2.0 * spread_per_side
        ret = pnl_per / p0 if p0 > 0 else 0.0
        equity *= (1.0 + ret)
        equity_curve.append(equity)
        trades.append({
            "entry": str(dates.iloc[entry_i]),
            "exit": str(dates.iloc[exit_i]),
            "side": in_pos,
            "reason": reason,
            "opt_type": opt_type,
            "expiry": str(expiry.date()) if expiry else None,
            "strike": strike,
            "entry_premium": round(p0, 2),
            "exit_premium": round(p_exit, 2),
            "entry_index": round(entry_index, 2),
            "exit_index": round(close[exit_i], 2),
            "pnl_per_option": round(pnl_per, 2),
            "ret_pct": round(ret * 100, 2),
            "bars": exit_i - entry_i,
        })
        in_pos = None

    def check_exit(i):
        """Check target/stop/eod for the current position and close if needed."""
        nonlocal in_pos, target_index, stop_index, entry_index, p0, strike, expiry, opt_type, opt_lot
        is_last = (i == n - 1 or
                   pd.Timestamp(dates.iloc[i]).normalize() !=
                   pd.Timestamp(dates.iloc[i + 1]).normalize())

        if in_pos == "call":
            hit_target = high[i] >= target_index
            hit_stop = low[i] <= stop_index
        else:
            hit_target = low[i] <= target_index
            hit_stop = high[i] >= stop_index

        if hit_target and hit_stop:
            if same_bar == "heuristic":
                if in_pos == "call":
                    hit_target = open_[i] >= entry_index
                    hit_stop = not hit_target
                else:
                    hit_target = open_[i] <= entry_index
                    hit_stop = not hit_target
            elif same_bar == "target":
                hit_stop = False
            else:  # stop-first
                hit_target = False

        if hit_target:
            p_exit = p0 * (1.0 + TARGET_PCT)
            close_trade(i, "target", p_exit)
            return True
        if hit_stop:
            p_exit = p0 * (1.0 - STOP_PCT)
            close_trade(i, "stop", p_exit)
            return True
        if is_last:
            day = pd.Timestamp(dates.iloc[i]).normalize()
            r = opt_row(frames, day, expiry, strike, opt_type)
            if r is not None and pd.notna(r["ClsPric"]) and r["ClsPric"] > 0:
                p_exit = float(r["ClsPric"])
            else:
                if in_pos == "call":
                    p_exit = p0 + DELTA * (close[i] - entry_index)
                else:
                    p_exit = p0 + DELTA * (entry_index - close[i])
            close_trade(i, "eod", p_exit)
            return True
        return False

    for i in range(1, n):
        if in_pos is None:
            if entry_mode == "close":
                call_break = hc[i] - hh[i - 1]
                put_break = hl[i - 1] - hc[i]
            else:
                # buy-stop / sell-stop: price touches the previous HA high/low
                call_break = high[i] - hh[i - 1]
                put_break = hl[i - 1] - low[i]
            call = call_break > 0
            put = put_break > 0

            if call and put:
                # Stronger absolute breakout wins
                if call_break >= put_break:
                    put = False
                else:
                    call = False

            if not call and not put:
                continue

            side = "call" if call else "put"
            opt_type = "CE" if call else "PE"
            entry_day = pd.Timestamp(dates.iloc[i]).normalize()

            opt = select_option(frames, entry_day, opt_type)
            if opt is None:
                # No option data for this day; skip
                continue

            p0 = opt["premium"]
            strike = opt["strike"]
            expiry = opt["expiry"]
            opt_lot = opt["lot"]
            if entry_mode == "close":
                entry_index = close[i]
            else:
                entry_index = hh[i - 1] if side == "call" else hl[i - 1]

            # Index points needed for a +/-5% option move, approximated with delta
            idx_distance = p0 * TARGET_PCT / DELTA
            if side == "call":
                target_index = entry_index + idx_distance
                stop_index = entry_index - idx_distance
            else:
                target_index = entry_index - idx_distance
                stop_index = entry_index + idx_distance

            in_pos = side
            entry_i = i

            # In break mode the stop/target can be hit in the same bar as the entry
            if entry_mode == "break" and check_exit(i):
                continue

        else:
            check_exit(i)

    if in_pos is not None:
        day = pd.Timestamp(dates.iloc[-1]).normalize()
        r = opt_row(frames, day, expiry, strike, opt_type)
        if r is not None and pd.notna(r["ClsPric"]) and r["ClsPric"] > 0:
            p_exit = float(r["ClsPric"])
        else:
            if in_pos == "call":
                p_exit = p0 + DELTA * (close[-1] - entry_index)
            else:
                p_exit = p0 + DELTA * (entry_index - close[-1])
        close_trade(n - 1, "eod", p_exit)

    return trades, equity_curve


def metrics(trades, years):
    if not trades:
        return {
            "total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0,
            "trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
            "avg_trade_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "time_in_market_pct": 0.0,
        }
    rets = np.array([t["ret_pct"] / 100.0 for t in trades])
    eq = np.cumprod(1.0 + rets)
    dd = (eq / np.maximum.accumulate(eq) - 1.0).min()
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pf = (wins.sum() / -losses.sum()) if losses.sum() < 0 else (np.inf if wins.sum() > 0 else 0.0)
    return {
        "total_return_pct": round((eq[-1] - 1.0) * 100, 2),
        "cagr_pct": round(((eq[-1]) ** (1.0 / years) - 1.0) * 100, 2) if years > 0 else np.nan,
        "max_drawdown_pct": round(dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(100.0 * len(wins) / len(trades), 1),
        "profit_factor": round(float(pf), 2) if np.isfinite(pf) else "inf",
        "avg_trade_pct": round(rets.mean() * 100, 2),
        "avg_win_pct": round(wins.mean() * 100, 2) if len(wins) else 0.0,
        "avg_loss_pct": round(losses.mean() * 100, 2) if len(losses) else 0.0,
    }


def run_one(csv_path, frames, timeframe_label, same_bar="target", entry_mode="close"):
    df = load_csv(csv_path)
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    results = []
    all_trades = {}
    for spread in SPREADS:
        trades, eq = backtest(df, frames, spread_per_side=spread, same_bar=same_bar,
                              entry_mode=entry_mode)
        m = metrics(trades, years)
        # attach per-spread metadata
        m["timeframe"] = timeframe_label
        m["same_bar_assumption"] = same_bar
        m["entry_mode"] = entry_mode
        m["spread_per_side_rs"] = spread
        m["variant"] = f"{timeframe_label} {entry_mode} {same_bar} spread={spread}"
        total_rupee_per_lot = sum(t["pnl_per_option"] * LOT for t in trades)
        m["total_rupees_per_lot"] = round(total_rupee_per_lot, 2)
        results.append(m)
        all_trades[f"{timeframe_label}_{entry_mode}_{same_bar}_{spread}"] = trades
    return results, all_trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=CSV5)
    parser.add_argument("--label", default="5m")
    parser.add_argument("--same-bar", default="target", choices=["target", "stop", "heuristic"])
    parser.add_argument("--entry-mode", default="close", choices=["close", "break"])
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()

    frames = load_bhavcopies(CACHE)
    if not frames:
        raise SystemExit("No BhavCopy data found in cache")

    results, trades = run_one(args.csv, frames, args.label, args.same_bar, args.entry_mode)

    cols = ["variant", "total_return_pct", "max_drawdown_pct", "trades",
            "win_rate_pct", "profit_factor", "avg_trade_pct", "avg_win_pct",
            "avg_loss_pct", "total_rupees_per_lot"]
    print(f"\nBacktest: {args.csv}")
    print(pd.DataFrame(results)[cols].to_string(index=False))

    with open(args.out, "w") as f:
        json.dump({
            "csv": args.csv,
            "target_pct": TARGET_PCT,
            "stop_pct": STOP_PCT,
            "delta": DELTA,
            "lot": LOT,
            "spreads_per_side": SPREADS,
            "same_bar_assumption": args.same_bar,
            "entry_mode": args.entry_mode,
            "results": results,
            "trades": trades,
            "note": "Intraday target/stop estimated with index points and delta=0.5; "
                    "EOD exit uses actual option close when available. Entry premium is "
                    "the option's daily close at the signal day.",
        }, f, indent=2, default=str)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
