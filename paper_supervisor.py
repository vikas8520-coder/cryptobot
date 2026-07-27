#!/usr/bin/env python3
"""
paper_supervisor.py — combined P2 paper-trade sampler for the equity + crypto bots.

EXTENDS apex_p2_supervisor.py: instead of watching only ApeX, this samples the four
bots built on the yfinance SMA engine (S&P 500, Nifty 50, ONGC, ITC, BTC) and
prints ONE compact report covering the whole paper track. Stdlib-only so it runs under
any Python (launchd/cron/Hermes) — pure READ of the shims, never touches an exchange
or live creds.

WHY (audit 2026-07-22): Vikas approved folding all new paper bots into one
recurring report so progress toward the P3 live-gate is visible in a single line per
run, not N separate cron emails. ApeX keeps its own sampler (different engine/shim).

Exit 0 normally; exit 2 if ANY sampled bot looks unhealthy (shim down / balance NaN
/ open trade stuck > N cycles / balance drifting >5% off seed without explanation).
"""
import json
import sys
import urllib.request
import urllib.error
import datetime
import os

# ---- CONFIG: each bot = (label, port, password, seed_balance_for_drift_check) ----
BOTS = [
    ("SPX",    8086, "pass8086", 1000.0),
    ("NIFTY",  8087, "pass8087", 48278.0),
    ("ONGC",    8088, "pass8088", 48278.0),
    ("ITC",     8089, "pass8089", 48278.0),
    ("BTC",     8091, "pass8091", 500.0),   # 8090 is the dashboard, not a bot shim
]
BLOBS = "/Users/vikasreddy/cryptobot/paper_supervisor.log"   # git-ignored (runtime state)
LOG_KEEP = 500                                          # lines of history to retain


def _get(port, pw, ep, timeout=5):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/{ep}")
    import base64
    tok = base64.b64encode(f"freqtrader:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def sample(label, port, pw, seed):
    """Return (lines, is_unhealthy) for one bot."""
    try:
        cfg = _get(port, pw, "show_config")
        bal = _get(port, pw, "balance")
        prof = _get(port, pw, "profit")
        st = _get(port, pw, "status")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return ([f"{label:6s} | SHIM UNREACHABLE on :{port} — {type(e).__name__}: {e}"], True)

    total = bal.get("total")
    healthy_bal = isinstance(total, (int, float)) and not isinstance(total, bool)
    open_n = len(st) if isinstance(st, list) else -1
    closed = prof.get("closed_trade_count", 0)
    win = prof.get("winrate", 0.0) or 0.0
    pnl = prof.get("profit_closed_percent", 0.0) or 0.0
    drift = (total - seed) if healthy_bal else 0.0

    problems = []
    if not healthy_bal:
        problems.append("balance-not-a-number")
    if open_n > 0:
        problems.append(f"{open_n}-open")
    if abs(drift) > max(50.0, seed * 0.05):   # >5% off seed (or >$50 abs for tiny BTC seed)
        problems.append(f"drift-{drift:+.0f}")

    lines = [f"{label:6s} | dry={cfg.get('dry_run')} bal={total if healthy_bal else 'NaN':.2f} "
             f"seed={seed:.0f} drift={drift:+.2f} closed={closed} win%={win:.0f} "
             f"pnl%={pnl:.2f} open={open_n}"]
    if problems:
        lines.append(f"       | FLAGS: {', '.join(problems)}")
    return (lines, bool(problems))


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    block = [f"PAPER TRACK | {stamp}"]
    any_unhealthy = False
    for label, port, pw, seed in BOTS:
        lines, bad = sample(label, port, pw, seed)
        any_unhealthy = any_unhealthy or bad
        block.extend(lines)

    text = "\n".join(block)
    print(text)

    # Rolling log (atomic append, trim) — P3 gate is measured in WEEKS.
    try:
        existing = []
        if os.path.exists(BLOBS):
            with open(BLOBS) as f:
                existing = f.read().splitlines()
        existing.append(text)
        trimmed = existing[-LOG_KEEP:]
        tmp = BLOBS + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(trimmed) + "\n")
        os.replace(tmp, BLOBS)
    except Exception as e:
        print(f"       | log-write-failed: {type(e).__name__}: {e}")

    return 0 if not any_unhealthy else 2


if __name__ == "__main__":
    sys.exit(main())
