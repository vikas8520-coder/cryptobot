#!/usr/bin/env python3
"""
apex_p2_supervisor.py — P2 paper-trade sampler for the ApeX DEX bot.

Reads the ApeX shim on :8085 over HTTP and prints a compact report. Stdlib-only so it
runs in any Python (launchd/cron/Hermes), no .venv or apexomni needed — it is a pure
READ of the shim, never touches the exchange or the live creds.

Why this exists (APEX_PLAN P2 gate): the P3 evidence bar is >=30 closed trades and
>=2-4 weeks of paper history that beats buy-and-hold. This script is the recurring
sampler that watches progress toward that gate and flags regressions (open trades
stuck, balance drifting from the $1000 seed, etc.).

Exit 0 normally; exit 2 if the bot looks unhealthy (shim down / balance NaN / stuck open).
"""
import json
import sys
import urllib.request
import urllib.error
import datetime
import os

PORT = 8085
AUTH = ("freqtrader", "pass8085")          # apexomni shim basic auth (local only)
BASE = f"http://127.0.0.1:{PORT}/api/v1"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apex_supervisor.log")
LOG_KEEP = 400                             # lines; one run ~4 lines, ~100 runs of history


def _get(ep, timeout=5):
    req = urllib.request.Request(f"{BASE}/{ep}")
    # basic auth header (urllib has no built-in)
    import base64
    tok = base64.b64encode(f"{AUTH[0]}:{AUTH[1]}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    try:
        cfg = _get("show_config")
        bal = _get("balance")
        prof = _get("profit")
        st = _get("status")
        daily = _get("daily?timescale=30")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"APEX P2 SUPERVISOR: SHIM UNREACHABLE on :{PORT} — {type(e).__name__}: {e}")
        return 2

    total = bal.get("total")
    healthy_bal = isinstance(total, (int, float)) and not isinstance(total, bool)
    open_n = len(st) if isinstance(st, list) else -1
    closed = prof.get("closed_trade_count", 0)
    win = prof.get("winrate", 0.0) or 0.0
    pnl = prof.get("profit_closed_percent", 0.0) or 0.0
    start = bal.get("starting_capital", 1000.0)
    drift = (total - start) if healthy_bal else 0.0

    # 30-day realized sparkline last value (most-recent-first -> first row is newest)
    eq_last = "n/a"
    try:
        eq_last = round(daily["data"][0].get("starting_balance", 0)
                         + daily["data"][0].get("abs_profit", 0), 2)
    except Exception:
        pass

    gate = "PASS" if (closed >= 30 and healthy_bal and open_n == 0) else "not-yet"
    problems = []
    if not healthy_bal:
        problems.append("balance-not-a-number")
    if open_n > 0:
        problems.append(f"{open_n}-open-stuck")
    if abs(drift) > 50:            # >5% off the $1000 seed without explanation
        problems.append(f"balance-drift-${drift:.0f}")

    lines = []
    lines.append(f"APEX P2 | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} | "
                 f"dry_run={cfg.get('dry_run')} state={cfg.get('state')} "
                 f"bal=${total if healthy_bal else 'NaN':.2f} seed=${start:.0f} drift=${drift:.2f}")
    lines.append(f"       | closed={closed} win%={win:.0f} pnl%={pnl:.2f} open={open_n} "
                 f"eq_now=${eq_last} gate={gate}")
    if problems:
        lines.append(f"       | FLAGS: {', '.join(problems)}")
    text = "\n".join(lines)
    print(text)

    # Rolling log (atomic append, trim to LOG_KEEP lines) so history survives between
    # the periodic cron runs — the P3 gate is measured in WEEKS, not minutes.
    try:
        existing = []
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH) as f:
                existing = f.read().splitlines()
        existing.append(text)
        trimmed = existing[-LOG_KEEP:]
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(trimmed) + "\n")
        os.replace(tmp, LOG_PATH)
    except Exception as e:
        print(f"       | log-write-failed: {type(e).__name__}: {e}")

    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())
