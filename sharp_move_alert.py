#!/usr/bin/env python3
"""
sharp_move_alert.py — the live version of the 3-bot example.

When BTC makes a sharp move (>=5% in 24h), Telegram a breakdown of how each of the
three bots reacted: who's long, who's short (offense), who's in cash (defense). Fires
ONCE per move, then re-arms when BTC calms back under 3%, so no spam.
"""
import json
import os
import re

import requests
from requests.auth import HTTPBasicAuth

import brake_alerts as ba   # reuse the reachable-exchange picker
from state_io import save_json, verified_send

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.conf")
STATE = os.path.join(BASE, "sharp_move_state.json")
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"

BOTS = [("Spot", 8080, "pass8080"), ("Futures", 8081, "pass8081"),
        ("Braked Hold", 8082, "pass8082")]
TRIGGER = 5.0    # |24h BTC move| that counts as "sharp"
RESET = 3.0      # re-arm once the move calms below this


def send(text):
    """Verified send — True only if Telegram confirmed delivery."""
    return verified_send(API, CHAT, text, timeout=15, feed_source="sharp")


def status(port, pw):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/status",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=6).json()
    except Exception:
        return None


def btc_24h():
    src, ex = ba.pick_exchange()
    if ex is None:
        return None, None
    o = ex.fetch_ohlcv("BTC/USDT", timeframe="1h", limit=26)
    closes = [c[4] for c in o]
    if len(closes) < 25:
        return None, src
    return (closes[-1] / closes[-25] - 1) * 100, src


def describe(name, st):
    if st is None:
        return f"• {name}: (offline)"
    if not st:
        if name == "Futures":
            return f"• {name}: FLAT / cash — no short setup triggered, sitting it out"
        return f"• {name}: CASH — no positions (protected / waiting to re-enter)"
    def coins(ts):
        return ",".join(t["pair"].split("/")[0] for t in ts)
    longs = [t for t in st if not t.get("is_short")]
    shorts = [t for t in st if t.get("is_short")]
    parts = []
    if shorts:
        parts.append(f"SHORT {coins(shorts)} ↓ (offense — profits from the fall)")
    if longs:
        parts.append(f"LONG {coins(longs)} ↑")
    return f"• {name}: " + " | ".join(parts)


def main():
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            pass
    armed = not state.get("fired", False)

    move, src = btc_24h()
    if move is None:
        print("no BTC data this run", flush=True)
        return
    print(f"BTC 24h move {move:+.1f}% (armed={armed}, source={src})", flush=True)

    if abs(move) >= TRIGGER and armed:
        arrow = "🔻 CRASH" if move < 0 else "🚀 SURGE"
        lines = [f"⚡ SHARP BTC MOVE: {move:+.1f}% in 24h {arrow}",
                 "Here's how your 3 bots reacted (the live 3-day example):", ""]
        for name, port, pw in BOTS:
            lines.append(describe(name, status(port, pw)))
        lines.append("")
        if move < 0:
            lines.append("💡 On a DOWN move: watch who PROTECTS (spot bots → cash) "
                         "vs who plays OFFENSE (only futures can short the fall).")
        else:
            lines.append("💡 On an UP move: the long holders ride it; anyone short gets squeezed.")
        # only disarm once delivery is CONFIRMED — a failed send retries next run
        if send("\n".join(lines)):
            state["fired"] = True
            print("ALERT sent", flush=True)
        else:
            print("ALERT send failed — staying armed to retry", flush=True)
    elif abs(move) < RESET:
        state["fired"] = False     # re-arm for the next move

    state["last_move"] = round(move, 2)
    save_json(STATE, state)        # atomic — see state_io.py


if __name__ == "__main__":
    main()
