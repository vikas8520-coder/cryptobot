#!/usr/bin/env python3
"""One-shot: report the futures bot's CLEAN post-universe-change data, then self-remove.

Scheduled ~4 days after the 2026-07-20 whitelist swap that gave futures its own coins
(DOGE/AVAX/LINK/DOT/NEAR/ATOM) so it no longer collides with spot on the concentration
breaker. Answers the two questions we care about:
  1. Did the concentration force-exits actually stop? (count force_exit trades opened
     AFTER the change vs before)
  2. What is LS2 doing on its OWN coins now that spot no longer takes priority?

Sends the answer to Telegram, then removes its own launchd job so it never fires again.
Reuses the tested /stats machinery (read-only SQLite) — no exchange calls.
"""
import os
import sqlite3
import subprocess
from datetime import datetime, timezone

import brake_alerts as ba
import stats_lib
import telegram_bot as t
from state_io import load_json, verified_send

BASE = os.path.dirname(os.path.abspath(__file__))
JOB = "com.vikas.futurescheck"
MARKER = os.path.join(BASE, "futures_universe_change.json")


def force_exits_split(change_iso):
    """(before, after) counts of force_exit trades relative to the universe-change time.
    stats_lib.BOTS['futures'] is a (label, dbfile) tuple; dbfile is repo-relative."""
    b = stats_lib.BOTS.get("futures")
    db = b[1] if isinstance(b, (list, tuple)) and len(b) > 1 else None
    if db and not os.path.isabs(db):
        db = os.path.join(BASE, db)
    if not db or not os.path.exists(db):
        return None, None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT close_date, exit_reason FROM trades WHERE is_open=0 AND exit_reason='force_exit'"
        ).fetchall()
        con.close()
    except Exception:
        return None, None
    before = after = 0
    for close_date, _ in rows:
        if not close_date:
            continue
        try:
            cd = str(close_date)[:19]
            after += (cd >= change_iso[:19]); before += (cd < change_iso[:19])
        except Exception:
            pass
    return before, after


def main():
    change_iso = load_json(MARKER, {}).get("changed_utc", "2026-07-20T19:35:26")

    before, after = force_exits_split(change_iso)
    if before is None:
        verdict = "• (couldn't read the force_exit split from the DB — check /stats futures manually)"
    elif after == 0:
        verdict = (f"• ✅ CLEAN: {after} concentration force-exits since the swap "
                   f"(was {before} before). The collisions stopped — LS2 now runs its own trades.")
    else:
        verdict = (f"• ⚠️ {after} force-exits still since the swap (was {before}). Likely the leftover "
                   f"XRP position, or spot/futures still overlapping — worth a look.")

    msg = ("🔎 FUTURES CLEAN-DATA CHECK\n"
           "(auto-scheduled 4 days after the 2026-07-20 universe swap → DOGE/AVAX/LINK/DOT/NEAR/ATOM)\n\n"
           f"{verdict}\n\n— futures, on its own coins now —\n"
           f"{t.cmd_stats(['futures'])}\n\n— all 3 bots —\n{t.cmd_stats([])}\n\n"
           "This was a one-time check; it won't repeat. Ping Claude for a deeper analysis.")
    verified_send(ba.API, ba.CHAT, msg, feed_source="report")
    print("sent futures clean-data check", flush=True)


def cleanup():
    """Remove the plist FIRST (so it can't re-fire), then bootout self."""
    la = os.path.expanduser(f"~/Library/LaunchAgents/{JOB}.plist")
    try:
        os.remove(la)
    except OSError:
        pass
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{JOB}"], capture_output=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
