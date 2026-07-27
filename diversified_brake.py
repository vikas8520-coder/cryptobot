#!/usr/bin/env python3
"""
diversified_brake.py — the whole braked portfolio's HOLD/CASH map, in one alert.

Tracks TWO versions of the 4-sleeve braked portfolio (Crypto + Stocks + Gold + Bonds):
  🇮🇳 INDIA (primary) — NIFTYBEES / GOLDBEES / LTGILTBEES, all NSE-listed and tradeable
       on ONE Zerodha account in INR (no LRS). The frontier_backtest_india.py study showed
       this cuts drawdown 44%→12% (Calmar 1.42) with near-zero cross-sleeve correlation.
  🇺🇸 US (reference) — SPY / GLD / TLT, needs LRS/TCS from India; kept for comparison.

Crypto sleeve = fraction of the crypto watchlist above its 200-day line (per-coin, CCXT).
Macro sleeves = each ETF's 200-day brake (yfinance, forming-bar dropped; NSE tickers use IST).

Forward-validation: every run appends the India portfolio's state (deployed % + each sleeve's
price & HOLD/CASH) to diversified_history.csv, so we accumulate REAL out-of-sample evidence
on the version we'd actually trade — the honest test the backtest can't give.

Alert fires only when the target allocation CHANGES. Signals only — act via your broker.
Audit-compliant: atomic state, verified sends, run lock, queue-before-commit.
"""
import csv
import io
import os
import sys
from datetime import datetime, timezone

import brake_alerts as ba
import macro_alerts as ma
from state_io import acquire_lock, load_json, save_json, save_text, verified_send

BASE = ba.BASE
STATE = os.path.join(BASE, "diversified_brake_state.json")
BOARD = os.path.join(BASE, "diversified_brake_board.json")       # dashboard reads this
PENDING = os.path.join(BASE, "diversified_brake_pending.json")
HISTORY = os.path.join(BASE, "diversified_history.csv")          # forward-validation log
API = ba.API
CHAT = ba.CHAT

# each variant = crypto + 3 macro sleeves, equal weight
IN_SLEEVES = [("Stocks", "NIFTYBEES.NS"), ("Gold", "GOLDBEES.NS"), ("Bonds", "LTGILTBEES.NS")]
US_SLEEVES = [("Stocks", "SPY"), ("Gold", "GLD"), ("Bonds", "TLT")]
HIST_FIELDS = ["date", "deployed", "crypto_frac",
               "stocks_state", "stocks_price", "gold_state", "gold_price",
               "bonds_state", "bonds_price"]


def send(text):
    return verified_send(API, CHAT, text, feed_source="portfolio")


def crypto_sleeve():
    """Crypto sleeve = fraction of the crypto watchlist above its 200-day line."""
    coins = load_json(ba.WATCHLIST, {"coins": ba.DEFAULT_WATCH}).get("coins", ba.DEFAULT_WATCH)
    src, ex = ba.pick_exchange()
    if ex is None:
        return None
    above = total = 0
    for c in coins:
        try:
            st, _, _ = ba.brake_state(ex, c)
        except Exception:
            continue
        total += 1
        above += (st == "above")
    if total == 0:
        return None
    return {"name": "Crypto", "kind": "crypto", "above": above, "total": total,
            "frac": above / total, "src": src}


def macro_sleeve(name, symbol):
    try:
        st, price, sma = ma.brake_state(symbol)
    except Exception as e:
        print(f"skip {symbol}: {e}", flush=True)
        return None
    return {"name": name, "kind": "macro", "symbol": symbol, "state": st,
            "frac": 1.0 if st == "above" else 0.0, "gap": (price / sma - 1) * 100,
            "price": price, "sma": sma}


def variant(crypto, defs):
    """One 4-sleeve portfolio = shared crypto sleeve + its 3 macro ETFs."""
    sleeves = [crypto]
    for name, sym in defs:
        m = macro_sleeve(name, sym)
        if m:
            sleeves.append(m)
    if len(sleeves) < 2:
        return None
    return {"sleeves": sleeves, "deployed": sum(s["frac"] for s in sleeves) / len(sleeves),
            "n_covered": len(sleeves)}


def build_map():
    c = crypto_sleeve()
    if c is None:
        return None
    india, us = variant(c, IN_SLEEVES), variant(c, US_SLEEVES)
    if india is None and us is None:
        return None
    return {"india": india, "us": us,
            "ts": datetime.now(timezone.utc).isoformat()}


def signature(m):
    """A CHANGE worth alerting: any sleeve's hold/cash (both variants), crypto count."""
    sig = {}
    for key in ("india", "us"):
        v = m.get(key)
        if not v:
            continue
        for s in v["sleeves"]:
            k = f"{key}:{s['name']}"
            sig[k] = f"{s['above']}/{s['total']}" if s["kind"] == "crypto" else s["state"]
    return sig


def _sleeve_line(s):
    if s["kind"] == "crypto":
        f = s["frac"]
        dot = "🟢" if f >= 0.5 else ("🟡" if f > 0 else "🔴")
        return f"  {dot} Crypto  {f*100:>3.0f}% in  ({s['above']}/{s['total']} above 200-day)"
    dot = "🟢" if s["state"] == "above" else "🔴"
    tag = "HOLD" if s["state"] == "above" else "CASH"
    return f"  {dot} {s['name']:<7}{tag}  ({s['symbol'].replace('.NS','')} {s['gap']:+.0f}% vs 200d)"


def _variant_block(title, v):
    dep = v["deployed"] * 100
    return [f"{title} — {dep:.0f}% deployed · {100-dep:.0f}% cash"] + [_sleeve_line(s) for s in v["sleeves"]]


def portfolio_text(m, header="🧭 DIVERSIFIED BRAKE — portfolio map"):
    lines = [header]
    if m.get("india"):
        lines += [""] + _variant_block("🇮🇳 INDIA · tradeable on Zerodha (INR, no LRS)", m["india"])
    if m.get("us"):
        lines += [""] + _variant_block("🇺🇸 US · reference (needs LRS from India)", m["us"])
    lines.append("\nEach sleeve ≈25% · idle cash earns yield · India ≈ ⅓ crypto's drawdown "
                 "(backtested). Signals — act via your broker · not advice")
    return "\n".join(lines)


def change_text(old_sig, m):
    new_sig = signature(m)
    moved = sorted({k.split(":")[1] for k, v in new_sig.items() if old_sig.get(k) != v})
    return "changed: " + ", ".join(moved) if moved else "updated"


def log_history(m):
    """Append the INDIA portfolio's daily state for forward-validation (same-day overwrite)."""
    v = m.get("india")
    if not v:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = {f: "" for f in HIST_FIELDS}
    row["date"] = today
    row["deployed"] = round(v["deployed"], 4)
    for s in v["sleeves"]:
        if s["kind"] == "crypto":
            row["crypto_frac"] = round(s["frac"], 4)
        else:
            k = s["name"].lower()
            row[f"{k}_state"] = s["state"]
            row[f"{k}_price"] = round(s["price"], 4)
    rows = []
    if os.path.exists(HISTORY):
        try:
            rows = list(csv.DictReader(open(HISTORY)))
        except Exception:
            rows = []
    if rows and rows[-1].get("date") == today:
        rows[-1] = row
    else:
        rows.append(row)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HIST_FIELDS)
    w.writeheader()
    w.writerows(rows)
    save_text(HISTORY, buf.getvalue())


def main():
    lock = acquire_lock("diversified_brake", base=BASE)

    # strict: a corrupt signature file must not read as a first run (which re-baselines
    # the whole map silently and swallows the allocation change it should have alerted)
    state = load_json(STATE, {}, strict=True)
    first_run = not state
    m = build_map()
    if m is None:
        print("insufficient data this run — skipping", flush=True)
        return

    save_json(BOARD, m)          # cache full map (both variants) for the dashboard
    log_history(m)               # forward-validation time series (India portfolio)
    sig = signature(m)
    old_sig = state.get("signature", {})
    changed = (sig != old_sig)
    dep_in = (m.get("india") or {}).get("deployed", 0) * 100
    print(f"India deployed {dep_in:.0f}% | changed={changed}", flush=True)

    # crash-safe: queue the alert BEFORE committing state (at-least-once delivery)
    if not first_run and changed:
        pending = load_json(PENDING, [], strict=True)
        pending.append({"text": portfolio_text(
            m, header=f"🧭 DIVERSIFIED BRAKE — allocation {change_text(old_sig, m)}")})
        if not save_json(PENDING, pending):
            # don't commit the new signature while the alert never reached the queue
            print("FATAL: could not queue allocation alert — state left uncommitted", flush=True)
            sys.exit(1)

    if not save_json(STATE, {"signature": sig, "ts": m["ts"]}):
        print("signature commit FAILED — change re-detected next run", flush=True)

    if first_run:
        send(portfolio_text(m, header="🧭 DIVERSIFIED BRAKE — now tracking India + US. Map:"))
        print("baseline established", flush=True)
        return

    pending = load_json(PENDING, [], strict=True)
    remaining = [x for x in pending if not send(x["text"])]
    if remaining != pending:
        if not save_json(PENDING, remaining):
            print("queue rewrite FAILED — delivered alert(s) may be re-sent next run", flush=True)
    if remaining:
        print(f"{len(remaining)} alert(s) undelivered — retry next run", flush=True)
    elif pending:
        print(f"{len(pending)} alert(s) delivered", flush=True)
    else:
        print("no allocation change — quiet", flush=True)


if __name__ == "__main__":
    main()
