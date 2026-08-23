#!/usr/bin/env python3
"""
nifty_paper_alert.py — paper-only alert bot for the refined 5m Nifty breakout rule.

Watches ^NSEI 5m candles from Yahoo Finance, applies the refined rule, and sends
a Telegram alert when a setup appears. Does NOT place any orders. Run with
`--live` to send real Telegram messages; without it, the alert is only printed.

Refined rule used here:
- Heikin-Ashi previous high/low break
- EMA(10) on High & Low no-trade zone
- Time window 10:00–14:30 IST
- 4% option target, 5% option stop
- Previous day's Heikin-Ashi trend as daily bias
- Fixed-fraction sizing is NOT applied in the alert; it reports lot count for a
  theoretical 2% account risk at the default ₹100,000 capital.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nifty_backtest.ha_prevbreak_options_backtest_filtered import (
    ha_candles, ema, load_bhavcopies, select_option, LOT, DELTA
)
from state_io import save_json, load_json, verified_send
import nifty_paper_order as porder
from nifty_brokers import get_broker, list_brokers, PaperBroker

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
BROKER_CONF = "/Users/vikasreddy/cryptobot/nifty_broker_config.json"
CACHE = "/Users/vikasreddy/cryptobot/nifty_backtest/cache"
STATE = "/Users/vikasreddy/cryptobot/nifty_paper_alert_state.json"

ALLOWED_START = dtime(10, 0)
ALLOWED_END = dtime(14, 30)
EMA_PERIOD = 10
TARGET_PCT = 0.04
STOP_PCT = 0.05
DEFAULT_CAPITAL = 100000.0
DEFAULT_RISK_PCT = 0.02


def load_telegram_conf():
    if not os.path.exists(CONF):
        return None, None
    c = open(CONF).read()
    tok = re.search(r'TG_TOKEN="([^"]+)"', c)
    chat = re.search(r'TG_CHAT="([^"]+)"', c)
    return (tok.group(1) if tok else None), (chat.group(1) if chat else None)


def fetch_5m():
    """Download the last ~5 days of 5m ^NSEI data from Yahoo Finance."""
    try:
        import yfinance as yf
        df = yf.download("^NSEI", period="5d", interval="5m", progress=False)
    except Exception as e:
        print("yfinance download failed:", e, flush=True)
        return None
    if df is None or df.empty:
        return None
    df = df.reset_index()
    # yfinance may return multi-index columns; flatten
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if c[1] == "" else c[0] for c in df.columns]
    df = df.rename(columns={"Datetime": "Date"})
    # Make sure we have the required columns
    for c in ("Open", "High", "Low", "Close"):
        if c not in df.columns:
            return None
    # IST is UTC+5:30. Yahoo returns UTC for NSE in some cases.
    # Convert to Asia/Kolkata so the time window is correct.
    if df["Date"].dt.tz is None:
        df["Date"] = df["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    else:
        df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata")
    return df


def daily_bias_map(df):
    """Map each trading day to the previous day's Heikin-Ashi trend."""
    daily = df.groupby(df["Date"].dt.normalize()).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    ).dropna()
    if len(daily) < 2:
        return {}
    d_ho, d_hc, _, _ = ha_candles(daily)
    days = sorted(daily.index)
    out = {}
    for i, d in enumerate(days):
        if i == 0:
            continue
        if d_hc[i - 1] > d_ho[i - 1]:
            out[d] = "call"
        elif d_hc[i - 1] < d_ho[i - 1]:
            out[d] = "put"
        else:
            out[d] = None
    return out


def format_index(idx):
    return f"{idx:.2f}"


def compute_signal(df, frames):
    n = len(df)
    if n < EMA_PERIOD + 2:
        return None
    ho, hc, hh, hl = ha_candles(df)
    ema_high = ema(df["High"], EMA_PERIOD).values
    ema_low = ema(df["Low"], EMA_PERIOD).values
    bias_map = daily_bias_map(df)

    # Use the most recent *completed* 5m bar, not the currently forming one.
    i = n - 2
    t = pd.Timestamp(df["Date"].iloc[i]).time()
    if t < ALLOWED_START or t > ALLOWED_END:
        return None

    call_break = df["High"].values[i] - hh[i - 1]
    put_break = hl[i - 1] - df["Low"].values[i]
    call = call_break > 0
    put = put_break > 0
    close_i = df["Close"].values[i]
    ema_ok = np.isfinite(ema_high[i]) and np.isfinite(ema_low[i])

    if call:
        call = (ema_ok and close_i > ema_high[i] and close_i > ema_low[i] and close_i > hh[i - 1])
    if put:
        put = (ema_ok and close_i < ema_high[i] and close_i < ema_low[i] and close_i < hl[i - 1])

    if call and put:
        if call_break >= put_break:
            put = False
        else:
            call = False

    if not call and not put:
        return None

    side = "call" if call else "put"
    opt_type = "CE" if call else "PE"
    entry_day = pd.Timestamp(df["Date"].iloc[i]).normalize()

    if bias_map.get(entry_day) is not None and bias_map.get(entry_day) != side:
        return None

    # Use the latest available bhavcopy day for option selection.
    frame_days = sorted(frames.keys())
    if not frame_days:
        return None
    opt_day = max([d for d in frame_days if d <= entry_day], default=frame_days[0])
    opt = select_option({opt_day: frames[opt_day]}, opt_day, opt_type)
    if opt is None:
        return None

    p0 = opt["premium"]
    strike = opt["strike"]
    expiry = opt["expiry"]
    entry_index = hh[i - 1] if side == "call" else hl[i - 1]
    target_idx_distance = p0 * TARGET_PCT / DELTA
    stop_idx_distance = p0 * STOP_PCT / DELTA
    if side == "call":
        target_index = entry_index + target_idx_distance
        stop_index = entry_index - stop_idx_distance
    else:
        target_index = entry_index - target_idx_distance
        stop_index = entry_index + stop_idx_distance

    p_exit_target = p0 * (1.0 + TARGET_PCT)
    p_exit_stop = p0 * (1.0 - STOP_PCT)

    # Lot size for a theoretical 2% risk on DEFAULT_CAPITAL.
    spread = 0.5  # assume conservative half-rupee per side for alert sizing
    risk_per_lot = LOT * (p0 * STOP_PCT + 2.0 * spread)
    lots = (DEFAULT_CAPITAL * DEFAULT_RISK_PCT) / risk_per_lot if risk_per_lot > 0 else 0.0

    dt = df["Date"].iloc[i]
    if dt.tzinfo is not None:
        dt = dt.tz_localize(None)
    return {
        "bar_time": str(dt),
        "side": side,
        "opt_type": opt_type,
        "strike": strike,
        "expiry": str(expiry.date()),
        "entry_premium": round(p0, 2),
        "target_premium": round(p_exit_target, 2),
        "stop_premium": round(p_exit_stop, 2),
        "entry_index": entry_index,
        "target_index": target_index,
        "stop_index": stop_index,
        "lots": round(lots, 2),
    }


def send_telegram(msg, live=False, source="nifty_paper_alert"):
    if not live:
        print("[DRY-RUN]", msg, flush=True)
        return True
    token, chat = load_telegram_conf()
    if not token or not chat:
        print("Telegram conf missing, alert not sent", flush=True)
        return False
    try:
        return verified_send(f"https://api.telegram.org/bot{token}", chat, msg,
                             timeout=8, feed_source=source)
    except Exception as e:
        print("send telegram error:", e, flush=True)
        return False


def send_alert(alert, live=False):
    msg = (
        f"🟡 NIFTY 5m PAPER ALERT\n"
        f"Time: {alert['bar_time']}\n"
        f"Direction: {'LONG CALL (CE)' if alert['side'] == 'call' else 'LONG PUT (PE)'}\n"
        f"Strike: {alert['strike']} | Expiry: {alert['expiry']}\n"
        f"Entry premium (ref): ₹{alert['entry_premium']}\n"
        f"Target: ₹{alert['target_premium']} (+4%)\n"
        f"Stop: ₹{alert['stop_premium']} (-5%)\n"
        f"Entry index: {format_index(alert['entry_index'])}\n"
        f"Target index: {format_index(alert['target_index'])}\n"
        f"Stop index: {format_index(alert['stop_index'])}\n"
        f"Theoretical lots (2% of ₹1L): {alert['lots']}\n"
        f"This is a PAPER alert. No real order placed."
    )
    return send_telegram(msg, live=live, source="nifty_paper_alert")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Send real Telegram alerts")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--broker", default="paper",
                        help=f"Broker adapter ({', '.join(list_brokers())})")
    parser.add_argument("--broker-config", default=BROKER_CONF,
                        help="JSON config with broker credentials")
    args = parser.parse_args()

    state = load_json(STATE, {}, strict=False)
    last_alert_bar = state.get("last_alert_bar")

    try:
        broker = get_broker(args.broker, config_path=args.broker_config)
    except Exception as e:
        print(f"broker init failed ({args.broker}): {e}; falling back to paper", flush=True)
        broker = PaperBroker()

    # Use a one-shot telegram send for the alert; the pool timeout prevents DNS stalls.
    while True:
        now = datetime.now().time()
        if not (ALLOWED_START <= now <= ALLOWED_END):
            if not args.once:
                print("outside trading window", now, flush=True)
            if args.once:
                break
            time.sleep(60)
            continue

        df = fetch_5m()
        if df is None:
            print("no data", flush=True)
            if args.once:
                break
            time.sleep(60)
            continue

        frames = load_bhavcopies(CACHE)

        # Process/close any existing paper positions first.
        if broker and hasattr(broker, "process_open_positions"):
            def close_sender(trade):
                return send_telegram(porder.format_pnl_msg(trade), live=args.live,
                                     source="nifty_paper_order")
            closed = broker.process_open_positions(df, send_fn=close_sender)
            if closed:
                print("paper positions closed:", [t["id"] for t in closed], flush=True)

        # Check for a new signal only if no position is still open.
        open_trades = broker.get_positions() if broker else []
        if open_trades:
            print("open paper trade(s); skipping new signal", flush=True)
            if args.once:
                break
            time.sleep(60)
            continue

        alert = compute_signal(df, frames)
        if alert is None:
            print("no signal", flush=True)
        else:
            bar_dt = pd.Timestamp(alert["bar_time"])
            age_min = (datetime.now() - bar_dt).total_seconds() / 60.0
            if age_min > 15:
                print(f"stale signal, age={age_min:.1f} min; skipping", flush=True)
                if args.once:
                    break
                time.sleep(60)
                continue
            bar_time = alert["bar_time"]
            if bar_time != last_alert_bar:
                if send_alert(alert, live=args.live):
                    last_alert_bar = bar_time
                    save_json(STATE, {"last_alert_bar": last_alert_bar})
                    print("alert sent for", bar_time, flush=True)
                    if broker:
                        lots = broker.place_order(alert)
                        print("paper order opened, lots", lots, "for", bar_time, flush=True)
                else:
                    print("alert send failed", flush=True)
            else:
                print("already alerted for", bar_time, flush=True)

        if args.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
