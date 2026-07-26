#!/usr/bin/env python3
"""
Trade notifier — watches BOTH trading bots and sends a Telegram message on
every trade OPEN and CLOSE (like Freqtrade's native trade alerts, but covering
both bots through their REST APIs).

State is persisted so restarts don't re-spam. On the very first run it baselines
the current trades silently (so you don't get flooded with history) and only
notifies on events AFTER that.
"""
import time
from local_secrets import api_pw

from freqtrade_api import get as api_get
from state_io import load_json, save_json, telegram_conf, verified_send

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
CHAT, API = telegram_conf(CONF)
STATE = "/Users/vikasreddy/cryptobot/notifier_state.json"
BOTS = [("Spot", 8080, api_pw(8080)), ("Futures", 8081, api_pw(8081))]
POLL = 45  # seconds


def send(text):
    """Verified send — True only if Telegram confirmed delivery."""
    return verified_send(API, CHAT, text, timeout=15, feed_source="trade")


def load_state():
    """(state, first_run) — an unreadable/absent state file means baseline silently."""
    st = load_json(STATE)
    return (st, False) if st is not None else ({}, True)


def main():
    state, first_run = load_state()
    for name, _, _ in BOTS:
        state.setdefault(name, {"opened": [], "closed": []})
    if first_run:
        print("first run: baselining current trades silently", flush=True)

    while True:
        for name, port, pw in BOTS:
            opened = set(state[name]["opened"])
            closed = set(state[name]["closed"])
            st = api_get(port, pw, "status")
            tr = api_get(port, pw, "trades")
            if st is None or tr is None:
                continue
            trades = tr.get("trades", []) if isinstance(tr, dict) else []

            # new OPENS (from live status) — only mark notified once delivery confirms
            for t in st:
                tid = t["trade_id"]
                if tid not in opened:
                    if first_run:
                        opened.add(tid)
                        continue
                    d = "SHORT" if t.get("is_short") else "LONG"
                    if send(f"🟢 OPENED — {name}: {t['pair']} {d} @ {t['open_rate']} "
                            f"(${t.get('stake_amount',0):.0f})"):
                        opened.add(tid)

            # new CLOSES (from trade history)
            for t in trades:
                if t.get("is_open"):
                    continue
                tid = t["trade_id"]
                if tid not in closed:
                    if first_run:
                        closed.add(tid)
                        continue
                    pr = t.get("profit_ratio") or 0    # can be null on odd closes
                    emoji = "🟢" if pr > 0 else "🔴"
                    if send(f"{emoji} CLOSED — {name}: {t['pair']}\n"
                            f"Entry {t['open_rate']} → Exit {t.get('close_rate')}\n"
                            f"Reason: {t.get('exit_reason')}\n"
                            f"P&L {pr*100:+.2f}% "
                            f"(${t.get('profit_abs',0) or 0:+.2f}) | {t.get('trade_duration')} min"):
                        closed.add(tid)

            state[name]["opened"] = sorted(opened)
            state[name]["closed"] = sorted(closed)

        save_json(STATE, state)    # atomic — see state_io.py
        first_run = False
        time.sleep(POLL)


if __name__ == "__main__":
    main()
