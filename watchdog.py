#!/usr/bin/env python3
"""
watchdog.py — silent guardian of the whole stack.

Every ~15 min it checks that the 3 bots, the dashboard, the brake data, and the
safety-layer daemons (guardian, trade notifier, TraderJoy) are alive and fresh.
It stays SILENT when everything is healthy and only pings Telegram when something
NEWLY breaks (or recovers) — so a message means "act now," never noise.

Audit fixes (2026-07-19):
  - a problem is only committed to state once its alert was CONFIRMED delivered,
    so an alert that fails to send (network down at wake — exactly when things
    break) is retried next run instead of being lost forever;
  - brake-data staleness gets a one-cycle grace period, because after a long Mac
    sleep this watchdog typically runs before the hourly brake job finishes —
    that race already produced a false 🚨/✅ pair in the logs;
  - the safety daemons are themselves watched via launchctl (a crash-looping
    guardian used to be invisible);
  - state writes are atomic (state_io).
"""
import json
from local_secrets import api_pw
import os
import re
import subprocess
import time

import requests
from requests.auth import HTTPBasicAuth

from state_io import save_json, verified_send

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.conf")
STATE = os.path.join(BASE, "watchdog_state.json")
BRAKE_STATE = os.path.join(BASE, "brake_alert_state.json")
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"

# 2026-07-20: futures (OKX) bot brought back for 3-bot data analysis — watch it again.
# 2026-07-20: added scalp (1m) + daytrade (15m) LAB bots — dry-run proof-of-fee-drag.
# tradenotifier stays retired (its fills surface in the activity feed instead).
BOTS = [("Spot", 8080, api_pw(8080)), ("Futures", 8081, api_pw(8081)),
        ("Braked Hold", 8082, api_pw(8082)),
        ("Scalp", 8083, api_pw(8083)), ("Day Trade", 8084, api_pw(8084))]
DASHBOARD = "http://127.0.0.1:8090/"
DAEMONS = ["com.vikas.guardian", "com.vikas.traderjoy"]
BRAKE_STALE_H = 12         # brake signal is daily; only flag if the job is truly dead
                           # (12h tolerates normal overnight Mac sleep without false alarms)


def send(text):
    """Verified send — True only if Telegram confirmed delivery."""
    return verified_send(API, CHAT, text, timeout=15, feed_source="watchdog")


def daemon_pids():
    """{label: has_pid} for our launchd daemons, via `launchctl list`."""
    out = {}
    try:
        txt = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] in DAEMONS:
                out[parts[2]] = parts[0] != "-"
    except Exception:
        pass
    return out


def _brake_job_enabled():
    """True only if the brake-alerts launchd job is actually loaded.

    The brake signal (brake_alert_state.json) is produced by com.vikas.brakealerts.
    That plist was intentionally retired to disabled_plists/ on 2026-07-20, so a
    stale/missing state file is EXPECTED, not a fault. Checking launchctl means this
    auto-reactivates if the job is ever reloaded — no code change needed.
    """
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
        return "com.vikas.brakealerts" in out
    except Exception:
        # if we can't tell, stay silent rather than cry wolf
        return False


def check():
    """Return {problem_key: human_reason} for everything currently broken."""
    broken = {}
    for name, port, pw in BOTS:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/v1/ping",
                             auth=HTTPBasicAuth("freqtrader", pw), timeout=6)
            if r.json().get("status") != "pong":
                broken[f"bot:{name}"] = "not responding"
        except Exception:
            broken[f"bot:{name}"] = "unreachable"
    try:
        if requests.get(DASHBOARD, timeout=6).status_code != 200:
            broken["dashboard"] = "bad status"
    except Exception:
        broken["dashboard"] = "unreachable"
    pids = daemon_pids()
    for label in DAEMONS:
        if not pids.get(label, False):
            broken[f"daemon:{label.split('.')[-1]}"] = "not running (launchctl)"
    if os.path.exists(BRAKE_STATE):
        age_h = (time.time() - os.path.getmtime(BRAKE_STATE)) / 3600
        if age_h > BRAKE_STALE_H and _brake_job_enabled():
            broken["brake-data"] = f"stale ({age_h:.1f}h old)"
    elif _brake_job_enabled():
        broken["brake-data"] = "missing"
    return broken


def main():
    st = {}
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
        except Exception:
            pass
    prev = set(st.get("broken", []))
    stale_pending = st.get("brake_stale_pending", False)

    now = check()

    # one-cycle grace for brake-data staleness: after a long sleep this watchdog
    # races the hourly brake job at wake — only alert if stale on 2 consecutive runs
    if "brake-data" in now and "stale" in now["brake-data"]:
        if not stale_pending and "brake-data" not in prev:
            stale_pending = True
            del now["brake-data"]          # grace period — recheck next run
        # else: still stale after grace -> treat as genuinely broken
    else:
        stale_pending = False

    now_set = set(now)
    newly = now_set - prev
    recovered = prev - now_set

    persisted = set(now_set)
    if newly:
        lines = ["🚨 WATCHDOG — problem detected:"] + \
                [f"• {k}: {now[k]}" for k in sorted(newly)]
        lines.append("\nCheck `launchctl list | grep vikas` and the logs in ~/cryptobot.")
        if not send("\n".join(lines)):
            # delivery unconfirmed: keep these OUT of state so they re-alert next run
            persisted -= newly
            print("breakage alert NOT delivered — will retry next run", flush=True)
    if recovered:
        if not send("✅ WATCHDOG — recovered: " + ", ".join(sorted(recovered))):
            # keep them in state so the recovery is re-announced next run
            persisted |= recovered
            print("recovery alert NOT delivered — will retry next run", flush=True)

    save_json(STATE, {"broken": sorted(persisted), "ts": time.time(),
                      "brake_stale_pending": stale_pending})
    print("broken:", sorted(now_set) or "none healthy", flush=True)


if __name__ == "__main__":
    main()
