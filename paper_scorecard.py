#!/usr/bin/env python3
"""
paper_scorecard.py — weekly keep/kill scorecard for the paper-trading test window.

PURPOSE: the desk is in a frozen-config test (baseline commit b14188d, started
2026-07-23). Humans negotiate with losing bots; this file doesn't. Every Sunday it
reads each bot's sqlite ledger, computes fee-honest stats on trades closed SINCE
the test started, applies pre-committed verdict rules, appends one CSV row per bot
(paper_scorecard.csv) and prints a digest. The CSV is the week-over-week record;
the digest is the glanceable answer.

VERDICT RULES (agreed 2026-07-23, do not renegotiate mid-test):
  - closed < min_trades and >= 8 weeks elapsed  -> TOO SLOW (design too rare for the clock)
  - closed < min_trades                          -> NO DATA YET
  - profit factor < 0.9                          -> KILL ZONE (kill or full rewrite)
  - profit factor < 1.2                          -> PROBATION (paper only, no live path)
  - otherwise                                    -> ON TRACK (live-candidate list)

Failure classes written against:
  - missing/locked sqlite file -> row still emitted with error note, never crashes the run
  - pre-test trades polluting the sample -> stats filter close_date >= TEST_START
    (lifetime numbers shown for context only)
  - profit factor divide-by-zero when there are no losers -> reported as 'inf'
Stdlib only (sqlite3/csv/json) so cron can run it with system python, same
portability rule as paper_supervisor.py.
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "paper_scorecard.csv")
TEST_START = "2026-07-23"      # frozen-config baseline (commit b14188d); resets only on a new batch
WEEKS_TOO_SLOW = 8             # under min_trades after this many weeks = design too slow

# (label, sqlite file, min closed trades for a verdict, ledger currency symbol)
# min_trades per the 2026-07-23 plan: swing/trend 40, scalp 80, daily-gated 5, paper equity 20.
# Currency matters: INR ledgers store close_profit_abs in rupees — printing them as $
# would be the same headline-currency lie the dashboard fix (a3d22d5) killed.
BOTS = [
    ("spot",      "tradesv3_trendfollow.sqlite", 40, "$"),
    ("futures",   "tradesv3_futures_ls2.sqlite", 40, "$"),
    ("daytrade",  "tradesv3_daytrade.sqlite",    40, "$"),
    ("scalp",     "tradesv3_scalp.sqlite",       80, "$"),
    ("braked",    "tradesv3_brakedhold.sqlite",   5, "$"),
    ("spx",       "tradesv3_spx.sqlite",         20, "$"),
    ("nifty",     "tradesv3_nifty.sqlite",       20, "Rs"),
    ("ongc",      "tradesv3_ongc.sqlite",        20, "Rs"),
    ("itc",       "tradesv3_itc.sqlite",         20, "Rs"),
    ("btc",       "tradesv3_btc.sqlite",         20, "$"),
]


def bot_stats(db_path):
    """Closed-trade stats split into lifetime and since-TEST_START buckets.
    Returns dict or {'error': ...} — a broken ledger must not kill the whole run."""
    if not os.path.exists(db_path):
        return {"error": "db missing"}
    try:
        # Plain connect (NOT the URI 'mode=ro' form). The URI form intermittently
        # throws "unable to open database file" on WAL-mode ledgers that still have
        # a stale -wal/-shm from a SIGINT-killed bot (e.g. tradesv3_daytrade.sqlite
        # after the 2026-07-24 mass stop). Plain connect reads WAL dbs fine, including
        # live ones (spot/brakedhold still run) — same read path freqtrade's API uses.
        con = sqlite3.connect(db_path, timeout=10)
        rows = con.execute(
            "SELECT close_profit_abs, close_date FROM trades "
            "WHERE is_open=0 AND close_profit_abs IS NOT NULL "
            "ORDER BY close_date"
        ).fetchall()
        con.close()
    except Exception as e:  # locked/corrupt/schema drift — report, don't raise
        return {"error": f"{type(e).__name__}: {e}"}

    life_n = len(rows)
    life_sum = sum(r[0] for r in rows)
    test = [r for r in rows if (r[1] or "") >= TEST_START]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    week_n = sum(1 for r in test if (r[1] or "") >= week_ago)

    pnl = [r[0] for r in test]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = float("inf") if gross_win > 0 else 0.0

    # max drawdown of the since-start equity curve (cumulative net P&L)
    peak = equity = 0.0
    max_dd = 0.0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {
        "test_n": len(pnl), "week_n": week_n,
        "test_pnl": sum(pnl),
        "expectancy": (sum(pnl) / len(pnl)) if pnl else 0.0,
        "win_rate": (len(wins) / len(pnl) * 100) if pnl else 0.0,
        "pf": pf, "max_dd": max_dd,
        "life_n": life_n, "life_pnl": life_sum,
    }


def verdict(s, min_trades, weeks_elapsed):
    if "error" in s:
        return "ERROR"
    if s["test_n"] < min_trades:
        return "TOO SLOW" if weeks_elapsed >= WEEKS_TOO_SLOW else "NO DATA YET"
    if s["pf"] < 0.9:
        return "KILL ZONE"
    if s["pf"] < 1.2:
        return "PROBATION"
    return "ON TRACK"


def main():
    now = datetime.now(timezone.utc)
    weeks = (now - datetime.strptime(TEST_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days / 7.0
    run_date = now.strftime("%Y-%m-%d")

    new_file = not os.path.exists(CSV_PATH)
    out_rows = []
    lines = [f"PAPER SCORECARD — {run_date} · week {weeks:.1f} of test (baseline {TEST_START})",
             "=" * 72]

    for name, db, min_trades, cur in BOTS:
        s = bot_stats(os.path.join(BASE, db))
        v = verdict(s, min_trades, weeks)
        if "error" in s:
            lines.append(f"{name:9s} ERROR — {s['error']}")
            out_rows.append([run_date, name, v, "", "", "", "", "", "", "", "", s["error"]])
            continue
        pf_txt = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
        # Native-currency printout: INR ledgers must not be shown as dollars
        # (same honest-headline rule that drove a3d22d5 / apex removal).
        lines.append(
            f"{name:9s} {v:11s} n={s['test_n']:3d} (+{s['week_n']} wk, need {min_trades}) "
            f"pf={pf_txt:>5s} exp={cur}{s['expectancy']:+.2f} win={s['win_rate']:.0f}% "
            f"dd={cur}{s['max_dd']:.2f} pnl={cur}{s['test_pnl']:+.2f} "
            f"(life n={s['life_n']} {cur}{s['life_pnl']:+.2f})"
        )
        out_rows.append([run_date, name, v, s["test_n"], s["week_n"], pf_txt,
                         f"{s['expectancy']:.4f}", f"{s['win_rate']:.1f}",
                         f"{s['max_dd']:.2f}", f"{s['test_pnl']:.2f}", cur, ""])

    lines.append("=" * 72)
    lines.append("rules: pf<0.9@min_n=KILL ZONE · pf<1.2=PROBATION · pf>=1.2=ON TRACK · "
                 f"<min_n after {WEEKS_TOO_SLOW}wk=TOO SLOW")
    lines.append(f"csv  -> {CSV_PATH}")
    digest = "\n".join(lines)
    print(digest)

    # append-only CSV — the durable week-over-week record
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "bot", "verdict", "test_trades", "week_trades", "pf",
                        "expectancy", "win_rate_pct", "max_dd", "test_pnl", "currency", "note"])
        w.writerows(out_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
