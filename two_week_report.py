#!/usr/bin/env python3
"""
Two-week forward-test report — a one-time scheduled Telegram summary comparing
the bots' state now vs. the baseline captured when the forward-test began.

Audit fix (2026-07-20): launchd's StartCalendarInterval has no YEAR field, so a
Month/Day trigger refires every year — and the old self-cleanup ran `bootout`
(which SIGTERMs this very process) BEFORE os.remove, so the plist was never deleted
and this fired again on the next July 31 against a stale baseline. Now it is (a)
IDEMPOTENT via a .done marker so a refire is a no-op, and (b) cleans up in the right
order — remove the plist FIRST, then bootout last (nothing needs to run after).
"""
import requests, re, json, os, subprocess
from local_secrets import api_pw
from requests.auth import HTTPBasicAuth
from datetime import datetime

from state_io import verified_send, save_text

BASE = "/Users/vikasreddy/cryptobot"
CONF = f"{BASE}/telegram.conf"
BASELINE = f"{BASE}/forward_test_baseline.json"
GUARD = f"{BASE}/guard_state.json"
DONE = f"{BASE}/two_week_report.done"
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.vikas.tworeport.plist")
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"
BOTS = [("Spot", 8080, api_pw(8080)), ("Futures", 8081, api_pw(8081))]


def send(text):
    return verified_send(API, CHAT, text, feed_source="report")


def api(port, pw, ep):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/{ep}",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=8).json()
    except Exception:
        return None


def snapshot():
    out = {}
    for name, port, pw in BOTS:
        p = api(port, pw, "profit") or {}
        b = api(port, pw, "balance") or {}
        out[name] = {"balance": b.get("total", 0),
                     "pct": p.get("profit_closed_percent", 0),
                     "trades": p.get("closed_trade_count", 0),
                     "winrate": p.get("winrate", 0)}
    return out


def cleanup():
    """One-shot: delete the plist FIRST (so it can't refire), then bootout last —
    bootout SIGTERMs us, so anything after it would never run."""
    save_text(DONE, datetime.now().isoformat())
    try:
        if os.path.exists(PLIST):
            os.remove(PLIST)
    except Exception as e:
        print("plist remove note:", e, flush=True)
    try:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.vikas.tworeport"],
                       capture_output=True)
    except Exception as e:
        print("bootout note:", e, flush=True)


def main():
    if os.path.exists(DONE):          # already ran once — a launchd refire is a no-op
        print("two-week report already sent; skipping (idempotent)", flush=True)
        cleanup()                     # make sure the stale plist is gone
        return

    base = json.load(open(BASELINE)) if os.path.exists(BASELINE) else {}
    now = snapshot()
    start = base.get("date", "?")

    lines = [f"📅 2-WEEK FORWARD-TEST REPORT — {datetime.now().strftime('%b %d, %Y')}",
             f"(started {start})", ""]
    for name, _, _ in BOTS:
        b0 = base.get(name, {})
        n = now.get(name, {})
        d_bal = n.get("balance", 0) - b0.get("balance", 0)
        d_tr = n.get("trades", 0) - b0.get("trades", 0)
        arrow = "🟢" if d_bal >= 0 else "🔴"
        lines.append(f"📊 {name}")
        lines.append(f"  Wallet: ${n.get('balance',0):.2f}  ({arrow} ${d_bal:+.2f} over 2wks)")
        lines.append(f"  P&L all-time: {n.get('pct',0):+.2f}% | win {n.get('winrate',0)*100:.0f}%")
        lines.append(f"  Trades in period: {d_tr}")
        lines.append("")

    if os.path.exists(GUARD):
        g = json.load(open(GUARD))
        brk = "TRIPPED ⚠️" if g.get("breaker_tripped") else "armed, never tripped ✓"
        lines.append(f"🛡️ Guardian: {brk} | peak ${g.get('peak_balance',0):.0f}")
        lines.append("")
    lines.append("💡 Reminder: judge on smoothness/drawdown, not profit — this is a "
                 "preservation/learning sandbox, not an income source. Come back to "
                 "Claude ('check on the crypto bots') for a deeper analysis.")
    delivered = send("\n".join(lines))

    # one-shot cleanup ONLY after confirmed delivery — otherwise let it fire once more
    if delivered:
        cleanup()
    else:
        print("report send failed — leaving job in place to retry", flush=True)


if __name__ == "__main__":
    main()
