#!/usr/bin/env python3
"""ha_prevbreak_options_backtest_filtered.py

Backtest the friend's Heikin-Ashi "previous high/low break" rule on 1m or 5m
NIFTY index data, mapped to actual NSE weekly option premiums around Rs 180,
with two added filters:

1. Time-of-day filter: skip the first 30 minutes (before 09:45 IST) and the
   last 30 minutes (after 15:00 IST) of each trading day.
2. EMA no-trade zone: EMA(34) on High and Low.  Only allow a new trade when the
   actual close is outside both EMAs and in the breakout direction:
   - Call: close > EMA(High), close > EMA(Low), close > previous HA high.
   - Put:  close < EMA(High), close < EMA(Low), close < previous HA low.

Rules:
- Compute Heikin-Ashi candles.
- Entry at bar i when HA close breaks above previous HA high  -> long CE.
- Entry at bar i when HA close breaks below previous HA low   -> long PE.
- Time and EMA filters are applied before a new trade is opened.
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
from datetime import datetime, time

def parse_time(s):
    return datetime.strptime(s, "%H:%M").time()

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CSV5 = os.path.join(HERE, "..", "data", "NSEI_index_5m.csv")
CSV1 = os.path.join(HERE, "..", "data", "NSEI_index_1m.csv")
CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(HERE, "ha_prevbreak_options_results.json")

TARGET_PCT = 0.05              # default; overridable via --target-pct
STOP_PCT = 0.05                # default; overridable via --stop-pct
DELTA = 0.50
LOT = 65
SPREADS = [0.0, 0.5, 1.0, 2.0, 3.0]  # default per-side spreads; overridable via --spreads
MIN_VOLUME = 1

EMA_PERIOD = 34
ALLOWED_START = time(9, 45)   # first 30 minutes skipped
ALLOWED_END = time(15, 0)     # last 30 minutes skipped

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
    # Make tz-naive and keep wall-clock time, matching the naive BhavCopy day keys.
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)
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


def ema(series, period):
    """EMA with a warm-up requirement; first `period-1` values are NaN."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


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


def backtest(df, frames, spread_per_side=0.0, same_bar="target", entry_mode="close",
             target_pct=TARGET_PCT, stop_pct=STOP_PCT, ema_period=EMA_PERIOD,
             allowed_start=ALLOWED_START, allowed_end=ALLOWED_END,
             trailing_be_pct=0.0, daily_bias=False,
             initial_capital=100000.0, risk_pct=0.02):
    """Run one backtest for a given per-side spread and same-bar assumption."""
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    open_ = df["Open"].values
    dates = df["Date"]
    n = len(df)

    ho, hc, hh, hl = ha_candles(df)

    # Daily Heikin-Ashi bias: previous day's HA close vs HA open decides allowed direction.
    day_bias = {}
    if daily_bias:
        daily = df.groupby(dates.dt.normalize()).agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last"
        }).dropna()
        if len(daily) >= 2:
            d_ho, d_hc, _, _ = ha_candles(daily)
            unique_days = sorted(daily.index)
            for i, d in enumerate(unique_days):
                if i == 0:
                    continue
                prev = unique_days[i - 1]
                if d_hc[i - 1] > d_ho[i - 1]:
                    day_bias[d] = "call"
                elif d_hc[i - 1] < d_ho[i - 1]:
                    day_bias[d] = "put"
                else:
                    day_bias[d] = None

    # EMA on High and Low series; first ema_period-1 values are NaN.
    ema_high = ema(df["High"], ema_period).values
    ema_low = ema(df["Low"], ema_period).values

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

    # Fixed-fraction position sizing state. `capital` is the running account balance.
    trades = []
    capital = initial_capital
    equity_curve = [capital]

    def close_trade(exit_i, reason, p_exit):
        nonlocal in_pos, capital
        pnl_per = p_exit - p0 - 2.0 * spread_per_side
        # Risk per lot at entry: stop-loss premium + round-trip spread.
        risk_per_lot = LOT * (p0 * stop_pct + 2.0 * spread_per_side)
        lots = (capital * risk_pct) / risk_per_lot if risk_per_lot > 0 else 0.0
        pnl_trade = pnl_per * lots * LOT
        ret = pnl_trade / capital if capital > 0 else 0.0
        capital *= (1.0 + ret)
        equity_curve.append(capital)
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
            "lots": round(lots, 2),
            "capital": round(capital, 2),
            "pnl_trade": round(pnl_trade, 2),
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

        # Trailing breakeven: once the trade reaches +trailing_be_pct option premium,
        # move the stop to the entry index so a reversal cannot turn a profit into a loss.
        if trailing_be_pct > 0 and in_pos is not None:
            be_index_distance = p0 * trailing_be_pct / DELTA
            if in_pos == "call" and high[i] >= entry_index + be_index_distance:
                stop_index = max(stop_index, entry_index)
            elif in_pos == "put" and low[i] <= entry_index - be_index_distance:
                stop_index = min(stop_index, entry_index)

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
            if in_pos == "call":
                p_exit = p0 + DELTA * (target_index - entry_index)
            else:
                p_exit = p0 + DELTA * (entry_index - target_index)
            close_trade(i, "target", p_exit)
            return True
        if hit_stop:
            if in_pos == "call":
                p_exit = p0 + DELTA * (stop_index - entry_index)
            else:
                p_exit = p0 + DELTA * (entry_index - stop_index)
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
            # 1. Time-of-day filter: no new trades before allowed_start or after allowed_end.
            t = pd.Timestamp(dates.iloc[i]).time()
            if t < allowed_start or t > allowed_end:
                continue

            if entry_mode == "close":
                call_break = hc[i] - hh[i - 1]
                put_break = hl[i - 1] - hc[i]
            else:
                # buy-stop / sell-stop: price touches the previous HA high/low
                call_break = high[i] - hh[i - 1]
                put_break = hl[i - 1] - low[i]
            call = call_break > 0
            put = put_break > 0

            # 2. EMA no-trade zone filter: close must be outside both EMAs
            #    and in the direction of the breakout.
            ema_ok = np.isfinite(ema_high[i]) and np.isfinite(ema_low[i])
            if call:
                call = (ema_ok and
                        close[i] > ema_high[i] and
                        close[i] > ema_low[i] and
                        close[i] > hh[i - 1])
            if put:
                put = (ema_ok and
                       close[i] < ema_high[i] and
                       close[i] < ema_low[i] and
                       close[i] < hl[i - 1])

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

            # 3. Daily-bias filter: only take trades aligned with the previous day's HA trend.
            if daily_bias and day_bias.get(entry_day) is not None and day_bias.get(entry_day) != side:
                continue

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

            # Index points needed for the target and stop option moves, approximated with delta
            target_idx_distance = p0 * target_pct / DELTA
            stop_idx_distance = p0 * stop_pct / DELTA
            if side == "call":
                target_index = entry_index + target_idx_distance
                stop_index = entry_index - stop_idx_distance
            else:
                target_index = entry_index - target_idx_distance
                stop_index = entry_index + stop_idx_distance

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


def metrics(trades, years, equity_curve=None, initial_capital=100000.0):
    if not trades:
        return {
            "total_return_pct": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0,
            "trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
            "avg_trade_pct": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "time_in_market_pct": 0.0, "total_profit": 0.0,
        }
    rets = np.array([t["ret_pct"] / 100.0 for t in trades])
    if equity_curve is not None and len(equity_curve) > 1:
        eq = np.array(equity_curve) / initial_capital
    else:
        eq = np.cumprod(1.0 + rets)
    dd = (eq / np.maximum.accumulate(eq) - 1.0).min()
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    gross_wins = sum(t["pnl_trade"] for t in trades if t["pnl_trade"] > 0)
    gross_losses = sum(t["pnl_trade"] for t in trades if t["pnl_trade"] <= 0)
    pf = (gross_wins / -gross_losses) if gross_losses < 0 else (np.inf if gross_wins > 0 else 0.0)
    total_profit = sum(t["pnl_trade"] for t in trades)
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
        "total_profit": round(total_profit, 2),
    }


def run_one(csv_path, frames, timeframe_label, same_bar="target", entry_mode="close",
            target_pct=TARGET_PCT, stop_pct=STOP_PCT, spreads=SPREADS, ema_period=EMA_PERIOD,
            allowed_start=ALLOWED_START, allowed_end=ALLOWED_END,
            start_date=None, end_date=None, trailing_be_pct=0.0, daily_bias=False,
            initial_capital=100000.0, risk_pct=0.02):
    df = load_csv(csv_path)
    if start_date is not None:
        df = df[df["Date"] >= pd.Timestamp(start_date)]
    if end_date is not None:
        df = df[df["Date"] <= pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)]
    years = (df["Date"].iloc[-1] - df["Date"].iloc[0]).days / 365.25
    results = []
    all_trades = {}
    for spread in spreads:
        trades, eq = backtest(df, frames, spread_per_side=spread, same_bar=same_bar,
                              entry_mode=entry_mode, target_pct=target_pct,
                              stop_pct=stop_pct, ema_period=ema_period,
                              allowed_start=allowed_start, allowed_end=allowed_end,
                              trailing_be_pct=trailing_be_pct, daily_bias=daily_bias,
                              initial_capital=initial_capital, risk_pct=risk_pct)
        m = metrics(trades, years, equity_curve=eq, initial_capital=initial_capital)
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
    parser.add_argument("--target-pct", type=float, default=TARGET_PCT,
                        help="Target as a fraction, e.g. 0.05 for 5%%")
    parser.add_argument("--stop-pct", type=float, default=STOP_PCT,
                        help="Stop loss as a fraction, e.g. 0.03 for 3%%")
    parser.add_argument("--spreads", type=float, nargs="+", default=SPREADS,
                        help="Per-side spread amounts in rupees")
    parser.add_argument("--ema-period", type=int, default=EMA_PERIOD,
                        help="EMA period for High/Low no-trade zone")
    parser.add_argument("--allowed-start", type=parse_time, default=ALLOWED_START,
                        help="Earliest time for new entries (HH:MM IST)")
    parser.add_argument("--allowed-end", type=parse_time, default=ALLOWED_END,
                        help="Latest time for new entries (HH:MM IST)")
    parser.add_argument("--trailing-be-pct", type=float, default=0.0,
                        help="Move stop to breakeven when option premium reaches this gain")
    parser.add_argument("--daily-bias", action="store_true",
                        help="Only take trades aligned with the previous day's Heikin-Ashi trend")
    parser.add_argument("--start-date", type=str, default=None,
                        help="Start date filter YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date filter YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100000.0,
                        help="Starting capital for fixed-fraction sizing")
    parser.add_argument("--risk-pct", type=float, default=0.02,
                        help="Risk per trade as a fraction of capital")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()

    frames = load_bhavcopies(CACHE)
    if not frames:
        raise SystemExit("No BhavCopy data found in cache")

    results, trades = run_one(args.csv, frames, args.label, args.same_bar, args.entry_mode,
                              target_pct=args.target_pct, stop_pct=args.stop_pct,
                              spreads=args.spreads, ema_period=args.ema_period,
                              allowed_start=args.allowed_start, allowed_end=args.allowed_end,
                              start_date=args.start_date, end_date=args.end_date,
                              trailing_be_pct=args.trailing_be_pct, daily_bias=args.daily_bias,
                              initial_capital=args.capital, risk_pct=args.risk_pct)

    cols = ["variant", "total_return_pct", "max_drawdown_pct", "trades",
            "win_rate_pct", "profit_factor", "avg_trade_pct", "avg_win_pct",
            "avg_loss_pct", "total_rupees_per_lot", "total_profit"]
    print(f"\nBacktest: {args.csv}")
    print(pd.DataFrame(results)[cols].to_string(index=False))

    with open(args.out, "w") as f:
        json.dump({
            "csv": args.csv,
            "target_pct": args.target_pct,
            "stop_pct": args.stop_pct,
            "ema_period": args.ema_period,
            "allowed_start": args.allowed_start.strftime("%H:%M"),
            "allowed_end": args.allowed_end.strftime("%H:%M"),
            "trailing_be_pct": args.trailing_be_pct,
            "daily_bias": args.daily_bias,
            "initial_capital": args.capital,
            "risk_pct": args.risk_pct,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "delta": DELTA,
            "lot": LOT,
            "spreads_per_side": args.spreads,
            "same_bar_assumption": args.same_bar,
            "entry_mode": args.entry_mode,
            "results": results,
            "trades": trades,
            "note": "Intraday target/stop estimated with index points and delta=0.5; "
                    "EOD exit uses actual option close when available. Entry premium is "
                    "the option's daily close at the signal day. "
                    "Filters: time-of-day (09:45-15:00 IST) and EMA(34) no-trade zone "
                    "on High/Low.",
        }, f, indent=2, default=str)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
