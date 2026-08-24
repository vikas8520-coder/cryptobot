#!/usr/bin/env python3
"""orderflow_inspired_backtest.py — Volume Profile + Value Area Reclaim + HA Reversal

Inspired by Chris Creamer's Robbins World Cup strategy, adapted for Nifty 5m
with only OHLCV data (no Level 2 order flow). This is Tier 1 of the strategy —
the structural framework without the order flow confirmation edge.

ADAPTATION FOR OPENING HOUR:
Chris Creamer draws Fibs on the developing intraday swing, but at 9:15 AM
there's no intraday swing yet. So instead of Fib retracement zones, we use
the PREVIOUS DAY's volume profile as the reference:
- If price opens BELOW the previous day's value area low = DISCOUNT
- If price opens ABOVE the previous day's value area high = PREMIUM
- The 0.886 Fib is kept as an invalidation/stop reference, not an entry zone

STRATEGY (adapted for Indian markets, opening hour only):
1. ENVIRONMENT: 15m EMA20 vs EMA50 determines up/down/sideways regime.

2. LOCATION: Previous day's volume profile defines the value area.
   CALL entries: price must be at or below VA low (discount) — buyers
   stepping in where price is "cheap" relative to yesterday's value.
   PUT entries: price must be at or above VA high (premium) — sellers
   stepping in where price is "expensive".

3. FIBONACCI INVALIDATION: Draw Fib on previous day's swing. The 0.886
   level is the hard invalidation — if price drops below 0.886 of the
   up-swing (or above 0.114 for down-swings), the setup is invalidated.

4. PARTICIPATION FILTER: Skip signals where the 5m candle range is below
   80% of the 20-bar average range. Low activity = unreliable.

5. ENTRY TRIGGER: Heikin-Ashi reversal candle in the discount/premium zone:
   - Bullish HA reversal (red→green) below VA low = CALL
   - Bearish HA reversal (green→red) above VA high = PUT

6. EXIT: 6% target, 2% stop. EOD exit if neither hit.

7. TIME WINDOW: 9:15-10:00 IST entry only. Exit monitoring until 15:30.

8. DAILY RISK: Max 2 trades per day. Stop after 2 consecutive losses.

HONEST CAVEATS:
- This is NOT Chris Creamer's strategy. It's the structural framework without
  the order flow confirmation that is his actual edge.
- Volume profile uses candle range (high-low) as volume proxy since our 5m
  data doesn't have a Volume column.
- "Absorption" is approximated by HA reversal (red candle followed by green)
  — this is a crude proxy, not true order flow.
- Backtest uses delta=0.5 approximation for option pricing.
"""
import os
import sys
from datetime import time, datetime
from collections import defaultdict

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ha_prevbreak_options_backtest_filtered import (
    load_csv, ha_candles, ema, load_bhavcopies, select_option,
    LOT, DELTA, CSV5, CACHE
)

SPREAD = 1.0
TARGET_PCT = 0.06
STOP_PCT = 0.02
MAX_TRADES_PER_DAY = 2
MAX_CONSEC_LOSSES = 2
EMA_FAST = 20   # 15m EMA fast for regime
EMA_SLOW = 50   # 15m EMA slow for regime
VOL_MA_LEN = 20 # participation filter lookback
VALUE_AREA_PCT = 0.70  # 70% of volume around POC
FIB_ENTRY_LOW = 0.705
FIB_ENTRY_HIGH = 0.886


def compute_volume_profile(df_day, bins=50):
    """Compute volume profile for a day's worth of 5m candles.
    Returns (poc, va_high, va_low) — point of control, value area high/low.

    If no Volume column (our 5m CSV doesn't have one), approximate volume
    using the candle range (high - low) as a proxy for activity."""
    if len(df_day) < 5:
        return None, None, None
    prices = df_day["Close"].values
    if "Volume" in df_day.columns:
        volumes = df_day["Volume"].values
        if volumes.sum() == 0:
            volumes = None
    else:
        volumes = None
    if volumes is None:
        # Approximate volume with candle range (high-low) as activity proxy
        volumes = (df_day["High"].values - df_day["Low"].values)
        if volumes.sum() == 0:
            volumes = np.ones(len(prices))

    price_min, price_max = prices.min(), prices.max()
    if price_max == price_min:
        return prices[0], prices[0], prices[0]

    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_indices = np.digitize(prices, bin_edges) - 1
    bin_indices = np.clip(bin_indices, 0, bins - 1)

    vol_by_bin = np.zeros(bins)
    for i, v in zip(bin_indices, volumes):
        vol_by_bin[i] += v

    poc_bin = np.argmax(vol_by_bin)
    poc = (bin_edges[poc_bin] + bin_edges[poc_bin + 1]) / 2

    # Value area: expand from POC until we capture VALUE_AREA_PCT of total volume
    total_vol = vol_by_bin.sum()
    target_vol = total_vol * VALUE_AREA_PCT
    va_vol = vol_by_bin[poc_bin]
    va_low_bin, va_high_bin = poc_bin, poc_bin

    while va_vol < target_vol and (va_low_bin > 0 or va_high_bin < bins - 1):
        # Expand to whichever side has more volume
        down_vol = vol_by_bin[va_low_bin - 1] if va_low_bin > 0 else 0
        up_vol = vol_by_bin[va_high_bin + 1] if va_high_bin < bins - 1 else 0
        if down_vol >= up_vol and va_low_bin > 0:
            va_low_bin -= 1
            va_vol += vol_by_bin[va_low_bin]
        elif va_high_bin < bins - 1:
            va_high_bin += 1
            va_vol += vol_by_bin[va_high_bin]
        else:
            break

    va_low = bin_edges[va_low_bin]
    va_high = bin_edges[va_high_bin + 1]
    return poc, va_high, va_low


def find_swing_high_low(df_day, lookback=10):
    """Find the day's swing high and low up to the current bar."""
    if len(df_day) < lookback:
        lookback = len(df_day)
    recent = df_day.iloc[-lookback:]
    swing_high = recent["High"].max()
    swing_low = recent["Low"].min()
    return swing_high, swing_low


def fib_levels(swing_low, swing_high):
    """Compute Fibonacci retracement levels."""
    diff = swing_high - swing_low
    if diff <= 0:
        return {}
    return {
        0.000: swing_high,
        0.236: swing_high - 0.236 * diff,
        0.382: swing_high - 0.382 * diff,
        0.500: swing_high - 0.500 * diff,
        0.618: swing_high - 0.618 * diff,
        0.705: swing_high - 0.705 * diff,
        0.788: swing_high - 0.788 * diff,
        0.886: swing_high - 0.886 * diff,
        1.000: swing_low,
    }


def ha_reversal(ho, hc, i, direction="bullish"):
    """Check for Heikin-Ashi reversal at bar i.
    Bullish: previous bars were red (close < open), current bar is green (close > open).
    Bearish: previous bars were green, current bar is red."""
    if i < 2:
        return False
    if direction == "bullish":
        # Current HA candle is bullish (close > open)
        if hc[i] <= ho[i]:
            return False
        # At least one of the previous 2 bars was bearish
        red_count = sum(1 for j in range(max(0, i-3), i) if hc[j] < ho[j])
        return red_count >= 1
    else:  # bearish
        if hc[i] >= ho[i]:
            return False
        green_count = sum(1 for j in range(max(0, i-3), i) if hc[j] > ho[j])
        return green_count >= 1


def run_backtest():
    df = load_csv(CSV5)
    n = len(df)
    if n < EMA_SLOW + 10:
        print("Not enough data")
        return

    ho, hc, hh, hl = ha_candles(df)

    # Build 15m EMA for regime detection (resample 5m to 15m)
    df["day"] = df["Date"].dt.normalize()
    agg_dict = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        agg_dict["Volume"] = "sum"
    df_15m = df.set_index("Date").resample("15min").agg(agg_dict).dropna().reset_index()
    ema20_15 = df_15m["Close"].ewm(span=EMA_FAST, adjust=False).mean().values
    ema50_15 = df_15m["Close"].ewm(span=EMA_SLOW, adjust=False).mean().values

    # Map 15m timestamps back to 5m indices for regime lookup
    regime_map = {}
    for j in range(len(df_15m)):
        ts = df_15m["Date"].iloc[j]
        if j >= EMA_SLOW and np.isfinite(ema20_15[j]) and np.isfinite(ema50_15[j]):
            if ema20_15[j] > ema50_15[j]:
                regime_map[ts] = "up"
            elif ema20_15[j] < ema50_15[j]:
                regime_map[ts] = "down"
            else:
                regime_map[ts] = "sideways"

    def get_regime(dt):
        """Get the 15m regime for a 5m timestamp."""
        # Round down to nearest 15 min
        ts = pd.Timestamp(dt).floor("15min")
        # Find the most recent 15m bar at or before this time
        for key in sorted(regime_map.keys(), reverse=True):
            if key <= ts:
                return regime_map[key]
        return "sideways"

    # Volume MA for participation filter — use candle range as proxy if no Volume
    if "Volume" in df.columns:
        vol_series = df["Volume"]
    else:
        vol_series = (df["High"] - df["Low"]).fillna(1)
    vol_ma = vol_series.rolling(VOL_MA_LEN).mean().values

    # Load BhavCopy for option pricing
    frames = load_bhavcopies(CACHE)
    if not frames:
        print("No BhavCopy data")
        return

    # Build previous-day volume profiles
    prev_day_vp = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        day_df = df[mask]
        if len(day_df) < 10:
            continue
        poc, va_high, va_low = compute_volume_profile(day_df)
        if poc is not None:
            prev_day_vp[day] = (poc, va_high, va_low)

    # Daily bias from previous day HA close
    bias_map = {}
    for day in sorted(df["day"].unique()):
        mask = df["day"] == day
        positions = np.where(mask.values)[0]
        if len(positions) < 2:
            continue
        ha_open_day = ho[positions[0]]
        ha_close_day = hc[positions[-1]]
        bias_map[day] = "call" if ha_close_day > ha_open_day else "put"

    # Walk through each bar, looking for entry signals
    trades = []
    daily_trade_count = defaultdict(int)
    daily_consec_losses = defaultdict(int)
    current_day = None

    for i in range(EMA_SLOW + 5, n - 1):
        t = pd.Timestamp(df["Date"].iloc[i]).time()
        entry_day = df["day"].iloc[i]

        # Reset daily counters on new day
        if entry_day != current_day:
            current_day = entry_day
            daily_trade_count[entry_day] = 0
            daily_consec_losses[entry_day] = 0

        # Entry window: 9:15-10:00 only
        if t < time(9, 15) or t > time(9, 59):
            continue

        # Daily limits
        if daily_trade_count[entry_day] >= MAX_TRADES_PER_DAY:
            continue
        if daily_consec_losses[entry_day] >= MAX_CONSEC_LOSSES:
            continue

        # Get previous day's volume profile
        prev_days = [d for d in prev_day_vp if d < entry_day]
        if not prev_days:
            continue
        prev_day = max(prev_days)
        poc, va_high, va_low = prev_day_vp[prev_day]
        if poc is None:
            continue

        # Get regime
        regime = get_regime(df["Date"].iloc[i])
        if regime == "sideways":
            continue

        # Previous day bias must match regime
        prev_bias_days = [d for d in bias_map if d < entry_day]
        if not prev_bias_days:
            continue
        prev_bias_day = max(prev_bias_days)
        bias = bias_map[prev_bias_day]

        # Swing high/low from previous day
        prev_day_mask = df["day"] == prev_day
        prev_day_df = df[prev_day_mask]
        swing_high, swing_low = find_swing_high_low(prev_day_df, lookback=len(prev_day_df))
        if swing_high <= swing_low:
            continue

        fibs = fib_levels(swing_low, swing_high)
        if not fibs:
            continue

        close_i = df["Close"].values[i]
        high_i = df["High"].values[i]
        low_i = df["Low"].values[i]

        # Participation filter
        if vol_ma is not None and np.isfinite(vol_ma[i]):
            cur_vol = vol_series.iloc[i]
            if cur_vol < vol_ma[i] * 0.8:  # below 80% of avg = low participation
                continue

        # Determine trade direction
        # Uptrend regime + call bias: look for CALL entries at discount (below POC)
        # Downtrend regime + put bias: look for PUT entries at premium (above POC)
        # Using POC (point of control = most traded price) as the discount/premium divider
        # instead of VA low/high — wider zone, more signals, still structurally sound.
        side = None
        if regime == "up" and bias == "call":
            # Price below POC = discount (cheap relative to yesterday's most-traded price)
            if close_i <= poc:
                # HA bullish reversal = buyers stepping in at discount
                if ha_reversal(ho, hc, i, "bullish"):
                    side = "call"

        elif regime == "down" and bias == "put":
            # Price above POC = premium (expensive relative to yesterday's most-traded price)
            if close_i >= poc:
                if ha_reversal(ho, hc, i, "bearish"):
                    side = "put"

        if side is None:
            continue

        opt_type = "CE" if side == "call" else "PE"

        # Option selection
        frame_days = sorted(frames.keys())
        opt_day = max([d for d in frame_days if d <= entry_day], default=frame_days[0] if frame_days else None)
        if opt_day is None:
            continue
        opt = select_option({opt_day: frames[opt_day]}, opt_day, opt_type)
        if opt is None:
            continue

        entry_premium = opt["premium"] + SPREAD
        if entry_premium <= 0:
            continue

        # Simulate outcome
        target_price = entry_premium * (1 + TARGET_PCT)
        stop_price = entry_premium * (1 - STOP_PCT)
        delta_sign = 1 if side == "call" else -1

        result = None
        exit_premium = None
        exit_bar = None

        for j in range(i + 1, min(i + 75, n)):
            bar_day = df["day"].iloc[j]
            if bar_day != entry_day:
                break
            index_move = df["Close"].values[j] - df["Close"].values[i]
            opt_move_pct = (index_move * DELTA / entry_premium) * 100
            est_premium = entry_premium * (1 + delta_sign * opt_move_pct / 100)

            if est_premium >= target_price:
                result = "target"
                exit_premium = target_price - SPREAD
                exit_bar = j
                break
            if est_premium <= stop_price:
                result = "stop"
                exit_premium = stop_price - SPREAD
                exit_bar = j
                break

        if result is None:
            exit_premium = entry_premium - SPREAD * 2
            result = "eod"

        pnl_pct = (exit_premium - entry_premium) / entry_premium * 100
        pnl_rs = (exit_premium - entry_premium) * LOT

        trades.append({
            "date": str(entry_day.date()),
            "time": str(t),
            "side": side,
            "regime": regime,
            "entry": entry_premium,
            "exit": exit_premium,
            "pnl_pct": pnl_pct,
            "pnl_rs": pnl_rs,
            "result": result,
            "bars_held": (exit_bar - i) if exit_bar else 75,
            "poc": poc,
            "va_high": va_high,
            "va_low": va_low,
            "fib_zone": f"{fibs.get(FIB_ENTRY_LOW, 0):.0f}-{fibs.get(FIB_ENTRY_HIGH, 0):.0f}" if side == "call" else "",
        })

        daily_trade_count[entry_day] += 1
        if pnl_pct <= 0:
            daily_consec_losses[entry_day] += 1
        else:
            daily_consec_losses[entry_day] = 0

    # Print results
    print(f"\n{'='*90}")
    print(f"ORDERFLOW-INSPIRED BACKTEST — Volume Profile + Fib + Participation Filter")
    print(f"Period: {df['Date'].iloc[0]} to {df['Date'].iloc[-1]}")
    print(f"Target: +{TARGET_PCT*100:.0f}%  Stop: -{STOP_PCT*100:.0f}%  Spread: ₹{SPREAD}/side  Lot: {LOT}")
    print(f"Entry window: 9:15-10:00 IST  Max trades/day: {MAX_TRADES_PER_DAY}  Max consec losses: {MAX_CONSEC_LOSSES}")
    print(f"{'='*90}\n")

    if not trades:
        print("NO TRADES GENERATED. The filters are too strict for this data period.")
        print("This is honest — it means the strategy didn't find enough setups.")
        return

    # Overall stats
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_pnl = sum(t["pnl_pct"] for t in trades) / len(trades)
    total_rs = sum(t["pnl_rs"] for t in trades)
    gross_w = sum(t["pnl_pct"] for t in wins)
    gross_l = abs(sum(t["pnl_pct"] for t in losses))
    pf = gross_w / gross_l if gross_l > 0 else float('inf')
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    # Max drawdown
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t["pnl_rs"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    print(f"{'Trades':>10} {'Wins':>6} {'Loss':>6} {'Win%':>7} {'Avg P&L%':>10} "
          f"{'Total ₹':>10} {'PF':>7} {'Avg Win%':>10} {'Avg Loss%':>10} {'Max DD ₹':>10}")
    print("-" * 100)
    print(f"{len(trades):>10} {len(wins):>6} {len(losses):>6} {win_rate:>6.1f}% "
          f"{avg_pnl:>+9.2f}% {total_rs:>+10.0f} {pf:>7.2f} {avg_win:>+9.2f}% {avg_loss:>+9.2f}% {max_dd:>10.0f}")

    # Per-side breakdown
    print(f"\n--- By side ---")
    for side in ["call", "put"]:
        side_trades = [t for t in trades if t["side"] == side]
        if not side_trades:
            print(f"  {side}: 0 trades")
            continue
        sw = [t for t in side_trades if t["pnl_pct"] > 0]
        sl = [t for t in side_trades if t["pnl_pct"] <= 0]
        swr = len(sw) / len(side_trades) * 100
        savg = sum(t["pnl_pct"] for t in side_trades) / len(side_trades)
        stotal = sum(t["pnl_rs"] for t in side_trades)
        sgw = sum(t["pnl_pct"] for t in sw)
        sgl = abs(sum(t["pnl_pct"] for t in sl))
        spf = sgw / sgl if sgl > 0 else float('inf')
        print(f"  {side}: {len(side_trades)} trades, {swr:.1f}% win, avg {savg:+.2f}%, ₹{stotal:+.0f}, PF {spf:.2f}")

    # Per-regime breakdown
    print(f"\n--- By regime ---")
    for regime in ["up", "down", "sideways"]:
        regime_trades = [t for t in trades if t["regime"] == regime]
        if not regime_trades:
            continue
        rw = [t for t in regime_trades if t["pnl_pct"] > 0]
        rwr = len(rw) / len(regime_trades) * 100
        rtotal = sum(t["pnl_rs"] for t in regime_trades)
        ravg = sum(t["pnl_pct"] for t in regime_trades) / len(regime_trades)
        print(f"  {regime}: {len(regime_trades)} trades, {rwr:.1f}% win, avg {ravg:+.2f}%, ₹{rtotal:+.0f}")

    # Time split: first half vs second half of data
    mid_date = df["Date"].iloc[n // 2].normalize()
    early = [t for t in trades if pd.Timestamp(t["date"]) < mid_date]
    late = [t for t in trades if pd.Timestamp(t["date"]) >= mid_date]
    print(f"\n--- Time split (out-of-sample check) ---")
    for label, subset in [("First half", early), ("Second half", late)]:
        if not subset:
            print(f"  {label}: 0 trades")
            continue
        sw = [t for t in subset if t["pnl_pct"] > 0]
        swr = len(sw) / len(subset) * 100
        stotal = sum(t["pnl_rs"] for t in subset)
        sgw = sum(t["pnl_pct"] for t in sw)
        sgl = abs(sum(t["pnl_pct"] for t in subset if t["pnl_pct"] <= 0))
        spf = sgw / sgl if sgl > 0 else float('inf')
        print(f"  {label}: {len(subset)} trades, {swr:.1f}% win, ₹{stotal:+.0f}, PF {spf:.2f}")

    # Trade detail
    print(f"\n{'='*90}")
    print("ALL TRADES")
    print(f"{'='*90}\n")
    print(f"{'Date':<12} {'Time':<10} {'Side':<5} {'Regime':<6} {'Entry':>7} {'Exit':>7} "
          f"{'P&L%':>8} {'₹':>8} {'Result':<8} {'Bars':>5}")
    for t in trades:
        print(f"{t['date']:<12} {t['time']:<10} {t['side']:<5} {t['regime']:<6} "
              f"₹{t['entry']:>5.0f} ₹{t['exit']:>5.0f} {t['pnl_pct']:>+7.2f}% "
              f"₹{t['pnl_rs']:>+7.0f} {t['result']:<8} {t['bars_held']:>5}")

    # Verdict
    print(f"\n{'='*90}")
    print("VERDICT")
    print(f"{'='*90}")
    if len(trades) < 20:
        print(f"⚠️  Only {len(trades)} trades — too few to be statistically significant.")
        print(f"   Need at least 30-50 trades to trust the edge.")
    if pf > 1.3 and len(trades) >= 20:
        print(f"✅ Profit factor {pf:.2f} with {len(trades)} trades — promising edge.")
        print(f"   But verify out-of-sample consistency above.")
    elif pf > 1.0:
        print(f"🟡 Profit factor {pf:.2f} — marginal edge. Not strong enough to deploy.")
        print(f"   The strategy barely beats breakeven. Consider tuning or more data.")
    else:
        print(f"❌ Profit factor {pf:.2f} — NO edge. The strategy loses money.")
        print(f"   This is honest: the structural framework without order flow is not enough.")


if __name__ == "__main__":
    run_backtest()
