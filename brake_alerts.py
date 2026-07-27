#!/usr/bin/env python3
"""
brake_alerts.py — the alert backend for "The Brake" (product piece A).

Once a day it checks each watched coin against its 200-day line and, WHEN THE STATE
FLIPS, sends the signal:  "🔴 go to cash"  or  "🟢 safe to hold".  Between flips it
says nothing — that's the product ("no charts to watch, one message when it matters").

Data source: CCXT, India-first. Binance (FIU-registered, works from an Indian IP) is
the intended production source; from a geo-blocked IP it falls back to the first
reachable liquid exchange. USDT prices are ~identical across venues (<0.1%), so the
200-day signal is the same one a CoinDCX/Binance-India trader would act on. The source
used is logged every run.

Signal is computed on the last CLOSED daily candle (today's forming candle is dropped)
so it fires once per confirmed daily close — no intraday flip-flopping.

MVP sends to the single Telegram chat in telegram.conf (you). The subscriber list —
per-user chat/email + their chosen coins — plugs in at SUBSCRIBERS below.
"""
import os
import sys
import time
from datetime import datetime, timezone

import ccxt
import requests

import state_io
from state_io import save_json, telegram_conf, verified_send

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.conf")
STATE = os.path.join(BASE, "brake_alert_state.json")
PENDING = os.path.join(BASE, "brake_pending_alerts.json")
WATCHLIST = os.path.join(BASE, "brake_watchlist.json")

MA_LEN = 200
FETCH_LIMIT = 260               # 200 for the SMA + buffer + the dropped forming candle
# India-first priority; auto-falls back to the first reachable one.
EXCHANGE_PRIORITY = ["binance", "okx", "kraken", "binanceus", "kucoin"]
DEFAULT_WATCH = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC"]

CHAT, API = telegram_conf(CONF)

# --- subscriber model (MVP = one chat). Replace with a DB/list to go multi-user. ---
SUBSCRIBERS = [{"channel": "telegram", "chat_id": CHAT, "coins": None}]  # coins None = all watched


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def send_telegram(chat_id, text):
    """Verified send — True only if Telegram confirmed delivery ({"ok": true})."""
    return verified_send(API, chat_id, text, feed_source="brake")


def load_json(path, default, strict=False):
    """Shared loader (state_io): missing -> default, unreadable -> logged / StateCorrupt."""
    return state_io.load_json(path, default, strict=strict)


def pick_exchange():
    """Return (name, exchange) for the first reachable priority exchange."""
    for name in EXCHANGE_PRIORITY:
        if name not in ccxt.exchanges:
            continue
        try:
            ex = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 10000})
            ex.fetch_ohlcv("BTC/USDT", timeframe="1d", limit=3)  # reachability probe
            return name, ex
        except Exception as e:
            log(f"exchange {name} unreachable ({str(e)[:60]}), trying next")
    return None, None


def brake_state(ex, coin):
    """Return (state, price, sma) for a coin using the last CLOSED daily candle.
    state is 'above' (hold) or 'below' (cash)."""
    ohlcv = ex.fetch_ohlcv(f"{coin}/USDT", timeframe="1d", limit=FETCH_LIMIT)
    closes = [c[4] for c in ohlcv][:-1]        # drop today's forming candle
    if len(closes) < MA_LEN:
        raise ValueError(f"{coin}: only {len(closes)} closed candles, need {MA_LEN}")
    price = closes[-1]
    sma = sum(closes[-MA_LEN:]) / MA_LEN
    return ("above" if price >= sma else "below"), price, sma


def fmt(v):
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.3f}" if v < 1 else f"${v:,.2f}"


def acquire_lock(name):
    """Shared lock (state_io), rooted at this module's BASE."""
    return state_io.acquire_lock(name, log=log, base=BASE)


def main():
    _lock = acquire_lock("brake_alerts")
    watch = load_json(WATCHLIST, {"coins": DEFAULT_WATCH}).get("coins", DEFAULT_WATCH)
    # strict: an unreadable state file must NOT masquerade as a first run — that path
    # re-baselines every coin and swallows the flip it was supposed to alert on
    state = load_json(STATE, {}, strict=True)   # {coin: {"state","price","sma","since"}}
    first_run = not state

    src_name, ex = pick_exchange()
    if ex is None:
        log("no reachable exchange; aborting this run")
        sys.exit(1)
    log(f"data source: {src_name} | watching: {', '.join(watch)}")

    now_iso = datetime.now(timezone.utc).isoformat()
    flips = []          # (coin, old, new, price, sma)
    snapshot = {}

    for coin in watch:
        try:
            st, price, sma = brake_state(ex, coin)
        except Exception as e:
            log(f"skip {coin}: {e}")
            # keep prior state if we have it
            if coin in state:
                snapshot[coin] = state[coin]
            continue
        prev = state.get(coin, {}).get("state")
        snapshot[coin] = {"state": st, "price": price, "sma": sma,
                          "since": state.get(coin, {}).get("since", now_iso)
                          if prev == st else now_iso}
        gap = (price / sma - 1) * 100
        log(f"  {coin}: {st.upper():5s} price {fmt(price)} vs 200d {fmt(sma)} ({gap:+.1f}%)")
        if prev is not None and prev != st:
            flips.append((coin, prev, st, price, sma))

    # CRASH-SAFE ORDERING (verified refutation fix): queue flip alerts into PENDING
    # BEFORE committing the new state. If we die between the two writes, the next
    # run re-detects the flip (old state) and re-queues — at-least-once delivery.
    # The reverse order lost the alert forever (state says "no change", queue empty).
    if not first_run and flips:
        import brake_memory
        pending = load_json(PENDING, [], strict=True)
        for coin, old, new, price, sma in flips:
            try:
                brake_memory.record_flip(coin, old, new, price, sma, ts=now_iso)
            except Exception as e:
                log(f"memory record failed for {coin}: {e} (alert still queued)")
            if new == "below":
                head = f"🔴 {coin}: BRAKE ON — go to cash"
                body = (f"{coin} closed BELOW its 200-day line ({fmt(price)} < {fmt(sma)}). "
                        f"The trend has broken — the brake says step aside until it reclaims "
                        f"the line. This is how you skip the worst of a downtrend.")
            else:
                head = f"🟢 {coin}: BRAKE OFF — safe to hold"
                body = (f"{coin} closed ABOVE its 200-day line ({fmt(price)} > {fmt(sma)}). "
                        f"The uptrend is back — the brake releases; holding again.")
            msg = f"{head}\n\n{body}\n\n— The Brake · not financial advice"
            for sub in SUBSCRIBERS:
                coins = sub.get("coins")
                if coins is not None and coin not in coins:
                    continue
                if sub["channel"] == "telegram":
                    pending.append({"chat_id": sub["chat_id"], "text": msg})
            log(f"ALERT queued: {coin} {old} -> {new}")
        if not save_json(PENDING, pending):
            # committing the new state now would tell the next run "no flip here" while
            # the alert never made it to the queue — abort instead and re-detect next run
            log("FATAL: could not queue flip alerts — leaving state uncommitted to retry")
            sys.exit(1)

    if not save_json(STATE, snapshot, indent=2):  # atomic — see state_io.py
        log("state commit FAILED — flips will be re-detected (and re-queued) next run")

    if first_run:
        # baseline quietly, but send a one-time "now watching" summary
        lines = ["🪂 THE BRAKE — alert backend is live.",
                 f"Watching {len(snapshot)} assets (source: {src_name}). "
                 f"Current brake status:"]
        for coin, d in snapshot.items():
            emoji = "🟢 HOLD" if d["state"] == "above" else "🔴 CASH"
            lines.append(f"  {emoji}  {coin}  ({fmt(d['price'])} vs 200d {fmt(d['sma'])})")
        lines.append("\nFrom now on you'll only hear from me when one of these FLIPS.")
        for sub in SUBSCRIBERS:
            if sub["channel"] == "telegram":
                send_telegram(sub["chat_id"], "\n".join(lines))
        log(f"baseline established for {len(snapshot)} coins; summary sent")
        return

    # Deliver EVERYTHING owed (new flips were queued above; plus retries from runs
    # where Telegram was down). Undelivered alerts survive in PENDING until confirmed
    # — "silence = all clear" must never be counterfeited by a swallowed send failure.
    pending = load_json(PENDING, [])
    remaining = [m for m in pending
                 if not send_telegram(m["chat_id"], m["text"])]
    if remaining != pending:
        if not save_json(PENDING, remaining):   # only rewrite when something was delivered
            log("queue rewrite FAILED — delivered alert(s) may be re-sent next run")
    if remaining:
        log(f"{len(remaining)} alert(s) NOT delivered — kept pending, retrying next run")
    elif pending:
        log(f"{len(pending)} alert(s) delivered (confirmed by Telegram)")
    else:
        log("no flips this run — staying quiet (as designed)")


if __name__ == "__main__":
    main()
