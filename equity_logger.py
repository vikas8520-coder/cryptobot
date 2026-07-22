#!/usr/bin/env python3
"""
equity_logger.py — the honest scoreboard.

Snapshots each bot's true equity alongside two buy-and-hold benchmarks (BTC and an
equal-weight basket) into equity_history.csv. Benchmarks are anchored to $1000 on the
first run, so everything is apples-to-apples with the bots' $1000 dry-run wallets.
Over weeks this answers the only question that matters — "is this actually helping?" —
with real forward data instead of a backtest. Overwrites the same-day row so the
series stays clean if run multiple times a day.
"""
import csv
from local_secrets import api_pw
import io
import json
import os

import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone

from state_io import save_json, save_text

BASE = os.path.dirname(os.path.abspath(__file__))
CSVF = os.path.join(BASE, "equity_history.csv")
STATE = os.path.join(BASE, "equity_logger_state.json")
BRAKE_STATE = os.path.join(BASE, "brake_alert_state.json")
START = 1000.0
BOTS = [("spot", 8080, api_pw(8080)), ("futures", 8081, api_pw(8081)),
        ("brakedhold", 8082, api_pw(8082)), ("apex", 8085, api_pw(8085)),
        ("spx", 8086, api_pw(8086)), ("nifty", 8087, api_pw(8087)),
        ("ongc", 8088, api_pw(8088)), ("itc", 8089, api_pw(8089)),
        ("btc", 8091, api_pw(8091))]   # 8091, NOT 8090 — 8090 is the dashboard
BASKET = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC",
          "DOGE", "LINK", "BNB", "AVAX", "DOT", "TRX"]
FIELDS = ["date", "spot", "futures", "brakedhold", "apex", "spx", "nifty", "ongc", "itc",
          "btc", "btc_hold", "basket_hold"]
# "btc" (the paper BOT's equity) is a different series from "btc_hold" (the buy-and-hold
# benchmark) — the bot is exactly what btc_hold is the control for. Do not merge them.


def balance(port, pw):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/balance",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=6).json().get("total")
    except Exception:
        return None


def prices():
    if os.path.exists(BRAKE_STATE):
        try:
            return {c: d.get("price") for c, d in json.load(open(BRAKE_STATE)).items()}
        except Exception:
            pass
    return {}


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    px = prices()

    st = {}
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}
    if "btc_start" not in st and px.get("BTC"):
        st = {"btc_start": px["BTC"], "start_date": today,
              "basket_start": {c: px[c] for c in BASKET if px.get(c)}}
        save_json(STATE, st, indent=2)

    row = {"date": today}
    for name, port, pw in BOTS:
        b = balance(port, pw)
        row[name] = round(b, 2) if b is not None else ""

    row["btc_hold"] = (round(START * px["BTC"] / st["btc_start"], 2)
                       if st.get("btc_start") and px.get("BTC") else "")
    bstart = st.get("basket_start") or {}
    ratios = [px[c] / bstart[c] for c in bstart if px.get(c) and bstart.get(c)]
    row["basket_hold"] = round(START * sum(ratios) / len(ratios), 2) if ratios else ""

    rows = []
    if os.path.exists(CSVF):
        rows = list(csv.DictReader(open(CSVF)))
    if rows and rows[-1]["date"] == today:
        # refresh today's row, but NEVER overwrite a good earlier value with "" —
        # a bot briefly offline on a later run used to blank the day's cell
        for k in FIELDS:
            if row.get(k) in (None, "") and rows[-1].get(k) not in (None, ""):
                row[k] = rows[-1][k]
        rows[-1] = row
    else:
        rows.append(row)
    # atomic write — one crash mid-write used to be able to destroy the whole
    # forward-test history (the file this experiment exists to produce)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    save_text(CSVF, buf.getvalue())
    print("logged:", row, flush=True)


if __name__ == "__main__":
    main()
