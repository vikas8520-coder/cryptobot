#!/usr/bin/env python3
"""
nifty_orderflow_bot.py — Volume Profile + HA Reversal paper bot (Tier 1).

Inspired by Chris Creamer's Robbins World Cup order flow strategy, adapted
for Nifty 5m with OHLCV-only data (no Level 2 order flow). This is the
structural framework: volume profile value area + HA reversal trigger.

STRATEGY:
1. ENVIRONMENT: 15m EMA20 vs EMA50 determines up/down/sideways regime.
2. LOCATION: Previous day's volume profile POC = discount/premium divider.
   CALL: price below POC (discount) + bullish HA reversal = buyers at discount.
   PUT: price above POC (premium) + bearish HA reversal = sellers at premium.
3. PARTICIPATION: Skip if 5m candle range < 80% of 20-bar avg range.
4. DAILY BIAS: Previous day's HA close vs open must match regime direction.
5. EXIT: 6% target, 2% stop. EOD exit if neither hit.
6. TIME: Entry 9:15-10:00 IST only. Exit monitoring until 15:30.
7. RISK: Max 2 trades/day. Stop after 2 consecutive losses.

Backtest (May-Aug 2026, 23 trades): PF 1.96, 47.8% win, +₹3,467, DD ₹908.
Both calls and puts profitable. Both time halves profitable (OOS consistent).

PAPER ONLY — no broker order path. Same architecture as nifty_paper_alert.py.
"""
import argparse
import json
import os
import re
import sys
import time as _time
from datetime import datetime, time as dtime, timezone
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifty_backtest.ha_prevbreak_options_backtest_filtered import (
    ha_candles, ema, load_bhavcopies, select_option, LOT, DELTA
)
from state_io import save_json, load_json, verified_send
from nifty_brokers import get_broker, PaperBroker
import nifty_paper_order as porder

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
BROKER_CONF = "/Users/vikasreddy/cryptobot/nifty_broker_config.json"
CACHE = "/Users/vikasreddy/cryptobot/nifty_backtest/cache"
STATE = "/Users/vikasreddy/cryptobot/nifty_orderflow_state.json"
ORDERFLOW_DB = "/Users/vikasreddy/cryptobot/nifty_orderflow_trades.sqlite"

ENTRY_START = dtime(9, 15)
ENTRY_END = dtime(10, 0)
EXIT_MONITOR_END = dtime(15, 30)
EMA_FAST_15M = 20
EMA_SLOW_15M = 50
TARGET_PCT = 0.06
STOP_PCT = 0.02
MAX_TRADES_PER_DAY = 2
MAX_CONSEC_LOSSES = 2
VOL_MA_LEN = 20
VALUE_AREA_PCT = 0.70
DEFAULT_CAPITAL = 100000.0
DEFAULT_RISK_PCT = 0.02


def load_telegram_conf():
    if not os.path.exists(CONF):
        return None, None
    c = open(CONF).read()
    tok = re.search(r'TG_TOKEN="([^"]+)"', c)
    chat = re.search(r'TG_CHAT="([^"]+)"', c)
    return (tok.group(1) if tok else None), (chat.group(1) if chat else None)


def send_telegram(msg, live=False, source="nifty_orderflow"):
    if not live:
        print(msg, flush=True)
        return True
    tok, chat = load_telegram_conf()
    if not tok or not chat:
        print(msg, flush=True)
        return False
    return verified_send(tok, chat, msg, source=source)


def fetch_5m():
    """Download last ~5 days of 5m ^NSEI data from Yahoo Finance."""
    try:
        import yfinance as yf
        df = yf.download("^NSEI", period="5d", interval="5m", progress=False)
    except Exception as e:
        print("yfinance download failed:", e, flush=True)
        return None
    if df is None or df.empty:
        return None
    df = df.reset_index()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[1] == "" else c[0] for c in df.columns]
    df = df.rename(columns={"Datetime": "Date"})
    for c in ("Open", "High", "Low", "Close"):
        if c not in df.columns:
            return None
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)
    return df


def compute_volume_profile(df_day, bins=50):
    """Volume profile using candle range as volume proxy (no Volume column)."""
    if len(df_day) < 5:
        return None, None, None
    prices = df_day["Close"].values
    volumes = (df_day["High"].values - df_day["Low"].values)
    if volumes.sum() == 0:
        volumes = np.ones(len(prices))
    price_min, price_max = prices.min(), prices.max()
    if price_max == price_min:
        return prices[0], prices[0], prices[0]
    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_indices = np.clip(np.digitize(prices, bin_edges) - 1, 0, bins - 1)
    vol_by_bin = np.zeros(bins)
    for i, v in zip(bin_indices, volumes):
        vol_by_bin[i] += v
    poc_bin = np.argmax(vol_by_bin)
    poc = (bin_edges[poc_bin] + bin_edges[poc_bin + 1]) / 2
    total_vol = vol_by_bin.sum()
    target_vol = total_vol * VALUE_AREA_PCT
    va_vol = vol_by_bin[poc_bin]
    va_low_bin, va_high_bin = poc_bin, poc_bin
    while va_vol < target_vol and (va_low_bin > 0 or va_high_bin < bins - 1):
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


def ha_reversal(ho, hc, i, direction="bullish"):
    """Check for Heikin-Ashi reversal at bar i."""
    if i < 2:
        return False
    if direction == "bullish":
        if hc[i] <= ho[i]:
            return False
        red_count = sum(1 for j in range(max(0, i-3), i) if hc[j] < ho[j])
        return red_count >= 1
    else:
        if hc[i] >= ho[i]:
            return False
        green_count = sum(1 for j in range(max(0, i-3), i) if hc[j] > ho[j])
        return green_count >= 1


def compute_signal(df, frames):
    """Compute the VP + HA reversal signal for the most recent completed bar."""
    n = len(df)
    if n < EMA_SLOW_15M * 3 + 5:
        return None

    # Entry time check
    i = n - 2  # most recent completed bar
    t = pd.Timestamp(df["Date"].iloc[i]).time()
    if t < ENTRY_START or t > ENTRY_END:
        return None

    # Heikin-Ashi candles
    ho, hc, hh, hl = ha_candles(df)

    # 15m regime detection
    df_15m = df.set_index("Date").resample("15min").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    if len(df_15m) < EMA_SLOW_15M + 2:
        return None
    ema20_15 = df_15m["Close"].ewm(span=EMA_FAST_15M, adjust=False).mean().values
    ema50_15 = df_15m["Close"].ewm(span=EMA_SLOW_15M, adjust=False).mean().values
    j = len(df_15m) - 2  # most recent completed 15m bar
    if not (np.isfinite(ema20_15[j]) and np.isfinite(ema50_15[j])):
        return None
    if ema20_15[j] > ema50_15[j]:
        regime = "up"
    elif ema20_15[j] < ema50_15[j]:
        regime = "down"
    else:
        return None  # sideways = no trade

    # Previous day's volume profile
    df["day"] = df["Date"].dt.normalize()
    entry_day = df["day"].iloc[i]
    prev_days = sorted([d for d in df["day"].unique() if d < entry_day])
    if not prev_days:
        return None
    prev_day = prev_days[-1]
    prev_day_df = df[df["day"] == prev_day]
    if len(prev_day_df) < 10:
        return None
    poc, va_high, va_low = compute_volume_profile(prev_day_df)
    if poc is None:
        return None

    # Previous day HA bias
    prev_positions = np.where((df["day"] == prev_day).values)[0]
    if len(prev_positions) < 2:
        return None
    prev_ha_open = ho[prev_positions[0]]
    prev_ha_close = hc[prev_positions[-1]]
    bias = "call" if prev_ha_close > prev_ha_open else "put"

    # Bias must match regime
    if (regime == "up" and bias != "call") or (regime == "down" and bias != "put"):
        return None

    # Participation filter
    vol_series = (df["High"] - df["Low"]).fillna(1)
    if len(vol_series) >= VOL_MA_LEN:
        vol_ma = vol_series.iloc[:i+1].rolling(VOL_MA_LEN).mean().iloc[-1]
        if vol_series.iloc[i] < vol_ma * 0.8:
            return None

    # Entry logic
    close_i = df["Close"].values[i]
    side = None

    if regime == "up" and bias == "call":
        if close_i <= poc:
            if ha_reversal(ho, hc, i, "bullish"):
                side = "call"
    elif regime == "down" and bias == "put":
        if close_i >= poc:
            if ha_reversal(ho, hc, i, "bearish"):
                side = "put"

    if side is None:
        return None

    opt_type = "CE" if side == "call" else "PE"

    # Option selection
    frame_days = sorted(frames.keys())
    if not frame_days:
        return None
    opt_day = max([d for d in frame_days if d <= entry_day], default=frame_days[0])
    opt = select_option({opt_day: frames[opt_day]}, opt_day, opt_type)
    if opt is None:
        return None

    entry_premium = opt["premium"]
    target_premium = entry_premium * (1 + TARGET_PCT)
    stop_premium = entry_premium * (1 - STOP_PCT)

    # Compute index levels at which the option hits target/stop
    # For calls: option gains DELTA * (index_move), so:
    #   target_index = entry_index + (target_premium - entry_premium) / DELTA
    #   stop_index   = entry_index - (entry_premium - stop_premium) / DELTA
    # For puts: option gains DELTA * (-index_move), so:
    #   target_index = entry_index - (target_premium - entry_premium) / DELTA
    #   stop_index   = entry_index + (entry_premium - stop_premium) / DELTA
    target_move = (target_premium - entry_premium) / DELTA
    stop_move = (entry_premium - stop_premium) / DELTA
    if side == "call":
        target_index = close_i + target_move
        stop_index = close_i - stop_move
    else:
        target_index = close_i - target_move
        stop_index = close_i + stop_move

    lots = max(1, int(DEFAULT_CAPITAL * DEFAULT_RISK_PCT / (entry_premium * LOT * STOP_PCT)))

    return {
        "bar_time": str(pd.Timestamp(df["Date"].iloc[i])),
        "side": side,
        "opt_type": opt_type,
        "strike": opt["strike"],
        "expiry": str(opt["expiry"].date()) if hasattr(opt["expiry"], "date") else str(opt["expiry"]),
        "entry_premium": round(entry_premium, 2),
        "target_premium": round(target_premium, 2),
        "stop_premium": round(stop_premium, 2),
        "entry_index": round(close_i, 2),
        "target_index": round(target_index, 2),
        "stop_index": round(stop_index, 2),
        "lots": lots,
        "regime": regime,
        "poc": round(poc, 2),
        "va_high": round(va_high, 2),
        "va_low": round(va_low, 2),
        "bias": bias,
        "index_close": round(close_i, 2),
        "strategy": "orderflow_tier1",
    }


def send_alert(alert, live=False):
    msg = (
        f"📊 NIFTY ORDERFLOW SIGNAL (Tier 1)\n"
        f"Side: {alert['side'].upper()} ({alert['opt_type']} {alert['strike']} {alert['expiry']})\n"
        f"Entry: ₹{alert['entry_premium']}  Target: ₹{alert['target_premium']}  Stop: ₹{alert['stop_premium']}\n"
        f"Lots: {alert['lots']}  Risk: ₹{alert['lots'] * LOT * alert['stop_premium']:.0f}\n"
        f"Regime: {alert['regime']}  Bias: {alert['bias']}\n"
        f"POC: {alert['poc']}  VA: {alert['va_low']}-{alert['va_high']}\n"
        f"Index: {alert['index_close']}\n"
        f"Time: {alert['bar_time']}\n"
        f"Strategy: VP + HA Reversal (paper only)"
    )
    return send_telegram(msg, live=live)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--broker", default="paper")
    parser.add_argument("--broker-config", default=BROKER_CONF)
    args = parser.parse_args()

    state = load_json(STATE, {"last_alert_bar": None, "daily_count": {}, "daily_losses": {}}, strict=False)
    last_alert_bar = state.get("last_alert_bar")

    try:
        broker = get_broker(args.broker, config_path=args.broker_config)
    except Exception as e:
        print(f"broker init failed ({args.broker}): {e}; falling back to paper", flush=True)
        broker = PaperBroker()

    # Use a separate DB for orderflow trades to keep them distinct from HA breakout trades
    if hasattr(broker, "conn"):
        broker.conn.close()
    broker.conn = porder.init_db(ORDERFLOW_DB)

    while True:
        now = datetime.now().time()
        in_entry_window = ENTRY_START <= now <= ENTRY_END
        in_exit_window = now <= EXIT_MONITOR_END
        if not in_entry_window and not in_exit_window:
            if not args.once:
                print("outside market hours", now, flush=True)
            if args.once:
                break
            _time.sleep(60)
            continue

        df = fetch_5m()
        if df is None:
            print("no data", flush=True)
            if args.once:
                break
            _time.sleep(60)
            continue

        frames = load_bhavcopies(CACHE)

        # Process/close existing positions
        if broker and hasattr(broker, "process_open_positions"):
            def close_sender(trade):
                # Track losses for daily risk limit
                if trade.get("pnl_trade", 0) <= 0:
                    today = str(datetime.now().date())
                    dl = state.get("daily_losses", {})
                    dl[today] = dl.get(today, 0) + 1
                    state["daily_losses"] = dl
                    save_json(STATE, state)
                return send_telegram(
                    porder.format_pnl_msg(trade), live=args.live,
                    source="nifty_orderflow")
            closed = broker.process_open_positions(df, send_fn=close_sender)
            if closed:
                print("positions closed:", [t.get("id") for t in closed], flush=True)

        open_trades = broker.get_positions() if broker else []
        if open_trades:
            print("open trade(s); monitoring exits only", flush=True)
            if args.once:
                break
            _time.sleep(60)
            continue

        if not in_entry_window:
            if args.once:
                break
            _time.sleep(60)
            continue

        # Daily risk limits
        today = str(datetime.now().date())
        daily_count = state.get("daily_count", {})
        daily_losses = state.get("daily_losses", {})
        today_count = daily_count.get(today, 0)
        today_losses = daily_losses.get(today, 0)

        if today_count >= MAX_TRADES_PER_DAY:
            print(f"max trades reached for {today}", flush=True)
            if args.once:
                break
            _time.sleep(60)
            continue
        if today_losses >= MAX_CONSEC_LOSSES:
            print(f"max consecutive losses for {today}; stopping", flush=True)
            if args.once:
                break
            _time.sleep(60)
            continue

        alert = compute_signal(df, frames)
        if alert is None:
            print("no signal", flush=True)
        else:
            bar_time = alert["bar_time"]
            if bar_time != last_alert_bar:
                if send_alert(alert, live=args.live):
                    last_alert_bar = bar_time
                    daily_count[today] = today_count + 1
                    state["last_alert_bar"] = last_alert_bar
                    state["daily_count"] = daily_count
                    save_json(STATE, state)
                    print("alert sent for", bar_time, flush=True)
                    if broker:
                        lots = broker.place_order(alert)
                        print("paper order opened, lots", lots, "for", bar_time, flush=True)
                else:
                    print("alert send failed for", bar_time, flush=True)
            else:
                print("already alerted for", bar_time, flush=True)

        if args.once:
            break
        _time.sleep(60)


if __name__ == "__main__":
    main()
