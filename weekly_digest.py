#!/usr/bin/env python3
"""
Weekly digest — a once-a-week Telegram summary of both trading bots + carry.
Scheduled via launchd (Sunday evening). Send-only; coexists with the listener.
"""
import requests, re, subprocess
from local_secrets import api_pw
from requests.auth import HTTPBasicAuth
from datetime import datetime

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"
PYBIN = "/Users/vikasreddy/cryptobot/.venv/bin/python3"
MONITOR = "/Users/vikasreddy/cryptobot/funding_monitor.py"
BOTS = [("Spot", 8080, api_pw(8080)), ("Futures", 8081, api_pw(8081))]


def send(text):
    requests.post(f"{API}/sendMessage", data={"chat_id": CHAT, "text": text}, timeout=20)


def api_get(port, pw, ep):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/{ep}",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=8).json()
    except Exception:
        return None


def bot_section(name, port, pw):
    prof = api_get(port, pw, "profit")
    bal = api_get(port, pw, "balance")
    daily = api_get(port, pw, "daily?timescale=7")
    st = api_get(port, pw, "status")
    if prof is None:
        return f"📊 {name}: offline"

    week_profit = 0.0
    week_trades = 0
    if daily and daily.get("data"):
        for d in daily["data"]:
            week_profit += d.get("abs_profit", 0) or 0
            week_trades += d.get("trade_count", 0) or 0

    lines = [f"📊 {name} bot"]
    if bal:
        lines.append(f"  Balance: ${bal.get('total', 0):.2f}")
    arrow = "🟢" if week_profit >= 0 else "🔴"
    lines.append(f"  This week: {arrow} ${week_profit:+.2f} over {week_trades} trades")
    lines.append(f"  All-time: {prof.get('profit_closed_percent', 0):+.2f}% | "
                 f"{prof.get('closed_trade_count', 0)} trades | "
                 f"win {prof.get('winrate', 0) * 100:.0f}%")
    if prof.get("best_pair"):
        lines.append(f"  Best pair: {prof.get('best_pair')} "
                     f"({prof.get('best_pair_profit_ratio', 0) * 100:+.1f}%)")
    lines.append(f"  Open now: {len(st) if st else 0}")
    return "\n".join(lines)


def carry_line():
    try:
        r = subprocess.run([PYBIN, MONITOR], capture_output=True, text=True, timeout=60)
        verdict = next((l.strip() for l in r.stdout.splitlines()
                        if any(k in l for k in ["STRONG", "MODEST", "SKIP"])), "n/a")
        return "📈 Funding carry: " + verdict
    except Exception:
        return "📈 Funding carry: check failed"


def main():
    now = datetime.now()
    parts = [f"📅 WEEKLY DIGEST — {now.strftime('%b %d, %Y')}", ""]
    for name, port, pw in BOTS:
        parts.append(bot_section(name, port, pw))
        parts.append("")
    parts.append(carry_line())
    parts.append("")
    parts.append("💡 Paper money — your learning/preservation sandbox. "
                 "Judge nothing on one week; regimes play out over months.")
    send("\n".join(parts))


if __name__ == "__main__":
    main()
