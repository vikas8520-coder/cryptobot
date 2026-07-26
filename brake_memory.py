#!/usr/bin/env python3
"""
brake_memory.py — the bots' memory (a trade journal), done safely.

This RECORDS what happened; it does NOT modify the trading rules. Two stores:
  - brake_journal.jsonl : append-only log of every 200-day flip (coin, direction, price)
  - brake_episodes.json : each "hold episode" (coin goes above its line -> back below)
    with its realized return, so a true track record accumulates over time.

The point is a reviewable history — "how have the brake's holds actually done?" —
that informs OUR decisions. It never changes what the bots do (that stays the fixed,
deterministic rule). Self-recording memory: safe. Self-modifying rules: not built, by design.
"""
import json
import os
from datetime import datetime, timezone

from state_io import load_json, save_json

BASE = os.path.dirname(os.path.abspath(__file__))
JOURNAL = os.path.join(BASE, "brake_journal.jsonl")
EPISODES = os.path.join(BASE, "brake_episodes.json")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _days(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
    except Exception:
        return 0


def _load():
    return load_json(EPISODES, {"open": {}, "closed": []})


def _save(e):
    save_json(EPISODES, e, indent=2)              # atomic — see state_io.py


def record_flip(coin, from_state, to_state, price, sma, ts=None, note=""):
    """Log a flip to the journal and open/close the coin's hold episode."""
    ts = ts or _now()
    with open(JOURNAL, "a") as f:
        f.write(json.dumps({"ts": ts, "coin": coin, "from": from_state,
                            "to": to_state, "price": price, "sma": sma, "note": note}) + "\n")
    e = _load()
    if to_state == "above":                       # a hold begins
        # if an episode is somehow still open (a 'below' flip was missed — crash,
        # corrupt state), close it first with a flag instead of silently clobbering it
        stale = e["open"].pop(coin, None)
        if stale and stale.get("entry_price"):
            e["closed"].append({
                "coin": coin, "entry_ts": stale["entry_ts"], "exit_ts": ts,
                "entry_price": stale["entry_price"], "exit_price": price,
                "ret_pct": (price / stale["entry_price"] - 1) * 100,
                "days": _days(stale["entry_ts"], ts), "note": "missed exit",
            })
        e["open"][coin] = {"entry_ts": ts, "entry_price": price}
    elif to_state == "below":                     # a hold ends -> record outcome
        op = e["open"].pop(coin, None)
        if op and op.get("entry_price"):
            e["closed"].append({
                "coin": coin, "entry_ts": op["entry_ts"], "exit_ts": ts,
                "entry_price": op["entry_price"], "exit_price": price,
                "ret_pct": (price / op["entry_price"] - 1) * 100,
                "days": _days(op["entry_ts"], ts),
            })
    _save(e)


def seed_open(coin, entry_ts, entry_price):
    """Record a hold that was already in progress before memory existed (no journal entry)."""
    e = _load()
    if coin not in e["open"]:
        e["open"][coin] = {"entry_ts": entry_ts, "entry_price": entry_price}
        _save(e)


def summary(current_prices=None):
    """Human-readable track record. current_prices={coin:price} adds live unrealized P&L."""
    e = _load()
    closed = e["closed"]
    lines = ["🧠 BRAKE MEMORY — track record"]

    if closed:
        wins = [c for c in closed if c["ret_pct"] > 0]
        avg = sum(c["ret_pct"] for c in closed) / len(closed)
        avgd = sum(c["days"] for c in closed) / len(closed)
        best = max(closed, key=lambda c: c["ret_pct"])
        worst = min(closed, key=lambda c: c["ret_pct"])
        lines += [
            f"Completed holds: {len(closed)} | win rate {len(wins)/len(closed)*100:.0f}% "
            f"| avg {avg:+.1f}% over ~{avgd:.0f}d",
            f"Best: {best['coin']} {best['ret_pct']:+.1f}% | Worst: {worst['coin']} {worst['ret_pct']:+.1f}%",
        ]
    else:
        lines.append("Completed holds: none yet — the record builds as coins flip back to cash.")

    op = e["open"]
    if op:
        lines.append("")
        lines.append(f"Currently holding ({len(op)}):")
        for coin, d in op.items():
            extra = ""
            if current_prices and coin in current_prices and d.get("entry_price"):
                unreal = (current_prices[coin] / d["entry_price"] - 1) * 100
                extra = f"  ({unreal:+.1f}% unrealized)"
            held = _days(d["entry_ts"], _now())
            lines.append(f"  {coin}: since {d['entry_ts'][:10]} (~{held}d){extra}")
    return "\n".join(lines)


def stats(current_prices=None):
    """Structured track record for the dashboard (mirror of summary()'s data)."""
    e = _load()
    closed = e["closed"]
    wins = [c for c in closed if c["ret_pct"] > 0]
    out = {
        "completed": len(closed),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "avg_ret": (sum(c["ret_pct"] for c in closed) / len(closed)) if closed else None,
        "avg_days": (sum(c["days"] for c in closed) / len(closed)) if closed else None,
        "best": max(closed, key=lambda c: c["ret_pct"]) if closed else None,
        "worst": min(closed, key=lambda c: c["ret_pct"]) if closed else None,
        "open": [],
    }
    for coin, d in e["open"].items():
        item = {"coin": coin, "since": d["entry_ts"][:10],
                "days": _days(d["entry_ts"], _now()), "unreal": None}
        if current_prices and coin in current_prices and d.get("entry_price"):
            item["unreal"] = (current_prices[coin] / d["entry_price"] - 1) * 100
        out["open"].append(item)
    return out


if __name__ == "__main__":
    print(summary())
