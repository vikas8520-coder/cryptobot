#!/usr/bin/env python3
"""
macro_alerts.py — the 200-day brake, applied to stocks / bonds / gold.

Same rule as the crypto brake, but on macro ETFs via free Yahoo Finance data — real
cross-asset diversification (US stocks, India stocks, bonds, gold all have different
risk drivers, unlike crypto's fake internal diversification). Telegram alert ONLY on a
flip (🔴 below its line / 🟢 back above); quiet otherwise. You act MANUALLY via your
broker (Zerodha etc.) — this is signals, not execution.
"""
import os
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from state_io import acquire_lock, load_json, save_json, telegram_conf, verified_send

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.conf")
STATE = os.path.join(BASE, "macro_alert_state.json")
PENDING = os.path.join(BASE, "macro_pending_alerts.json")
WATCHLIST = os.path.join(BASE, "macro_watchlist.json")
MA_LEN = 200

# Market session per symbol: (timezone, close hour, close minute). Yahoo's daily feed
# includes the STILL-FORMING bar during market hours — judging it would break the
# closed-candle rule the crypto brake enforces, so we drop it until the session ends.
MARKETS = {"^NSEI": ("Asia/Kolkata", 15, 30)}
DEFAULT_MARKET = ("America/New_York", 16, 0)      # SPY, QQQ, TLT, GLD (NYSE/Arca)

CHAT, API = telegram_conf(CONF)


def send(text):
    """Verified send — True only if Telegram confirmed delivery."""
    return verified_send(API, CHAT, text, feed_source="macro")


def brake_state(symbol):
    """(state, price, sma) for a ticker from its last CONFIRMED daily close vs its
    200-day SMA. Today's still-forming bar (present in Yahoo's feed during market
    hours) is dropped — the signal only ever judges a completed session, exactly
    like the crypto brake drops the forming daily candle."""
    df = yf.download(symbol, period="2y", interval="1d", progress=False, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError("no data")
    close = df["Close"]
    if hasattr(close, "columns"):        # yfinance sometimes returns a 1-col frame
        close = close.iloc[:, 0]
    close = close.dropna()

    if symbol in MARKETS:
        tzname, ch, cm = MARKETS[symbol]
    elif symbol.endswith(".NS"):            # any NSE-listed ETF closes 15:30 IST
        tzname, ch, cm = "Asia/Kolkata", 15, 30
    else:
        tzname, ch, cm = DEFAULT_MARKET
    now_l = datetime.now(ZoneInfo(tzname))
    last_date = close.index[-1].date()
    session_over = (now_l.hour * 60 + now_l.minute) >= (ch * 60 + cm + 15)  # +15min settle
    if last_date >= now_l.date() and not session_over:
        close = close.iloc[:-1]          # drop the in-progress bar

    closes = close.tolist()
    if len(closes) < MA_LEN:
        raise ValueError(f"only {len(closes)} bars, need {MA_LEN}")
    price = closes[-1]
    sma = sum(closes[-MA_LEN:]) / MA_LEN
    return ("above" if price >= sma else "below"), price, sma


def fmt(v):
    return f"{v:,.2f}"


def main():
    # serialize runs — interleaved manual+scheduled runs could clobber the pending queue
    _lock = acquire_lock("macro_alerts")

    assets = load_json(WATCHLIST, {"assets": []}).get("assets", [])
    names = {a["symbol"]: a["name"] for a in assets}
    state = load_json(STATE, {})
    first_run = not state

    now_iso = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat()
    flips, snapshot = [], {}

    for a in assets:
        sym = a["symbol"]
        try:
            st, price, sma = brake_state(sym)
        except Exception as e:
            print(f"skip {sym}: {e}", flush=True)
            if sym in state:
                snapshot[sym] = state[sym]
            continue
        prev = state.get(sym, {}).get("state")
        snapshot[sym] = {"state": st, "price": price, "sma": sma,
                         "since": state.get(sym, {}).get("since", now_iso) if prev == st else now_iso}
        gap = (price / sma - 1) * 100
        print(f"  {sym:6} {names.get(sym,''):22} {st.upper():5} {fmt(price)} vs 200d {fmt(sma)} ({gap:+.1f}%)", flush=True)
        if prev is not None and prev != st:
            flips.append((sym, prev, st, price, sma))

    # CRASH-SAFE ORDERING: queue flip alerts into PENDING BEFORE committing state —
    # dying between the writes then re-detects the flip next run (at-least-once).
    if not first_run and flips:
        pending = load_json(PENDING, [])
        for sym, old, new, price, sma in flips:
            nm = names.get(sym, sym)
            if new == "below":
                head = f"🔴 {nm}: BRAKE ON — trend broken"
                body = (f"{nm} closed BELOW its 200-day line ({fmt(price)} < {fmt(sma)}). "
                        f"Downtrend — the brake says lighten up / step aside until it reclaims the line.")
            else:
                head = f"🟢 {nm}: BRAKE OFF — uptrend back"
                body = (f"{nm} closed ABOVE its 200-day line ({fmt(price)} > {fmt(sma)}). "
                        f"Uptrend resumed — safe to hold again.")
            pending.append({"text": f"{head}\n\n{body}\n\n— Macro Brake · act via your broker · not financial advice"})
            print(f"ALERT queued: {sym} {old} -> {new}", flush=True)
        save_json(PENDING, pending)

    save_json(STATE, snapshot, indent=2)          # atomic — see state_io.py

    if first_run:
        lines = ["📈 MACRO BRAKE — now watching stocks / bonds / gold.",
                 "Current status (200-day trend brake):"]
        for a in assets:
            d = snapshot.get(a["symbol"])
            if not d:
                continue
            emoji = "🟢 HOLD" if d["state"] == "above" else "🔴 CASH"
            lines.append(f"  {emoji}  {a['name']}  ({fmt(d['price'])} vs 200d {fmt(d['sma'])})")
        lines.append("\nFrom now on you'll only hear from me when one FLIPS. Act via your broker.")
        send("\n".join(lines))
        print("baseline established", flush=True)
        return

    # deliver everything owed (new flips queued above + retries from earlier failures)
    pending = load_json(PENDING, [])
    remaining = [m for m in pending if not send(m["text"])]
    if remaining != pending:
        save_json(PENDING, remaining)
    if remaining:
        print(f"{len(remaining)} alert(s) NOT delivered — retrying next run", flush=True)
    elif pending:
        print(f"{len(pending)} alert(s) delivered", flush=True)
    else:
        print("no flips — staying quiet", flush=True)


if __name__ == "__main__":
    main()
