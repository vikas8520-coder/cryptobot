#!/usr/bin/env python3
"""
stats_lib.py — trade-analysis queries straight against Freqtrade's SQLite trade store.

Answers the questions the REST API can't in one shot: win rate BY EXIT REASON (where is
the money actually lost?), average hold time, profit factor, fees paid, and per-pair P&L.
Reads the .sqlite files READ-ONLY (mode=ro + busy timeout) so it never disturbs the live
bot writing to them — and it works even when a bot is stopped, unlike the REST API.

This is the payoff of "we already have SQL": the trade ledger is a real relational DB,
so a few SELECTs turn it into a scoreboard. No new storage, no schema of our own.
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))

# key -> (label, sqlite file). Mapping matches each bot's --db-url in its launchd plist.
BOTS = {
    "spot":       ("Spot · TrendFollow (1h)", "tradesv3_trendfollow.sqlite"),
    "futures":    ("Futures · LS2 (4h)",      "tradesv3_futures_ls2.sqlite"),
    "brakedhold": ("Braked Hold (1d)",        "tradesv3_brakedhold.sqlite"),
    "scalp":      ("Scalp · VWAP 5m LAB",     "tradesv3_scalp.sqlite"),
    "daytrade":   ("Day Trade · ORB 1h LAB",  "tradesv3_daytrade.sqlite"),
    "apex":       ("ApeX · Omni DEX",         "tradesv3_apex.sqlite"),
    "spx":        ("S&P 500 · SPY paper",      "tradesv3_spx.sqlite"),
}


def _conn(db):
    path = os.path.join(BASE, db)
    if not os.path.exists(path):
        return None
    # read-only URI + timeout: we are a guest in the bot's database, never a writer
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


SUMMARY_SQL = """
SELECT
  COUNT(*)                                                        AS n,
  SUM(CASE WHEN close_profit_abs > 0 THEN 1 ELSE 0 END)          AS wins,
  COALESCE(SUM(close_profit_abs), 0)                             AS net_abs,
  AVG(close_profit)                                              AS avg_ratio,
  MAX(close_profit)                                              AS best,
  MIN(close_profit)                                              AS worst,
  AVG(CASE WHEN close_profit_abs > 0 THEN close_profit END)      AS avg_win,
  AVG(CASE WHEN close_profit_abs <= 0 THEN close_profit END)     AS avg_loss,
  COALESCE(SUM(CASE WHEN close_profit_abs > 0 THEN close_profit_abs ELSE 0 END), 0) AS gross_win,
  COALESCE(SUM(CASE WHEN close_profit_abs < 0 THEN -close_profit_abs ELSE 0 END), 0) AS gross_loss,
  AVG((julianday(close_date) - julianday(open_date)) * 24.0)     AS avg_hrs,
  COALESCE(SUM(COALESCE(fee_open_cost,0) + COALESCE(fee_close_cost,0)), 0) AS fees,
  COALESCE(SUM(COALESCE(funding_fees,0)), 0)                     AS funding,
  SUM(CASE WHEN is_short = 1 THEN 1 ELSE 0 END)                  AS shorts
FROM trades WHERE is_open = 0;
"""

BY_REASON_SQL = """
SELECT exit_reason,
       COUNT(*)                                              AS n,
       SUM(CASE WHEN close_profit_abs > 0 THEN 1 ELSE 0 END) AS wins,
       COALESCE(SUM(close_profit_abs), 0)                    AS net,
       AVG(close_profit)                                     AS avg_ratio
FROM trades WHERE is_open = 0
GROUP BY exit_reason ORDER BY net ASC;
"""

BY_PAIR_SQL = """
SELECT pair,
       COUNT(*)                          AS n,
       COALESCE(SUM(close_profit_abs),0) AS net
FROM trades WHERE is_open = 0
GROUP BY pair ORDER BY net DESC;
"""

OPEN_SQL = "SELECT COUNT(*), COALESCE(SUM(stake_amount),0) FROM trades WHERE is_open = 1;"


def bot_stats(key):
    """Return a dict of analysis for one bot, or {'error': ...}. Read-only."""
    label, db = BOTS[key]
    c = _conn(db)
    if c is None:
        return {"label": label, "error": "db not found"}
    try:
        s = c.execute(SUMMARY_SQL).fetchone()
        reasons = c.execute(BY_REASON_SQL).fetchall()
        pairs = c.execute(BY_PAIR_SQL).fetchall()
        n_open, open_stake = c.execute(OPEN_SQL).fetchone()
    except sqlite3.Error as e:
        return {"label": label, "error": f"query failed: {e}"}
    finally:
        c.close()

    (n, wins, net_abs, avg_ratio, best, worst, avg_win, avg_loss,
     gross_win, gross_loss, avg_hrs, fees, funding, shorts) = s
    n = n or 0
    return {
        "label": label, "key": key, "closed": n, "wins": wins or 0,
        "net_abs": net_abs or 0.0, "avg_ratio": avg_ratio, "best": best, "worst": worst,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "gross_win": gross_win or 0.0, "gross_loss": gross_loss or 0.0,
        "avg_hrs": avg_hrs, "fees": fees or 0.0, "funding": funding or 0.0,
        "shorts": shorts or 0,
        "n_open": n_open or 0, "open_stake": open_stake or 0.0,
        "reasons": [{"reason": r[0] or "?", "n": r[1], "wins": r[2],
                     "net": r[3] or 0.0, "avg_ratio": r[4]} for r in reasons],
        "pairs": [{"pair": p[0], "n": p[1], "net": p[2] or 0.0} for p in pairs],
    }


def fmt_dur(hrs):
    if hrs is None:
        return "—"
    if hrs >= 24:
        return f"~{hrs/24:.1f}d"
    if hrs >= 1:
        return f"~{hrs:.1f}h"
    return f"~{hrs*60:.0f}m"


def _pf(gw, gl):
    if gl <= 0:
        return "∞" if gw > 0 else "—"
    return f"{gw/gl:.2f}"


def money(v):
    """Signed dollars, sign OUTSIDE the $: -$16.02, +$5.00."""
    return f"-${abs(v):.2f}" if v < 0 else f"+${v:.2f}"


def summary_line(d):
    """One compact block per bot for the all-bots view."""
    if d.get("error"):
        return f"• {d['label']}: {d['error']}"
    if d["closed"] == 0:
        tail = f"in cash by design ({d['n_open']} open, ${d['open_stake']:.0f})" \
            if d["n_open"] else "no trades yet"
        return f"• {d['label']}\n    0 closed · {tail}"
    wr = d["wins"] / d["closed"] * 100
    return (f"• {d['label']}\n"
            f"    {d['closed']} closed · {wr:.0f}% win · {money(d['net_abs'])} · "
            f"hold {fmt_dur(d['avg_hrs'])}")


def detail(d):
    """Full breakdown for one bot: summary + by-exit-reason + by-pair."""
    if d.get("error"):
        return f"📊 {d['label']}\n{d['error']}"
    if d["closed"] == 0:
        tail = (f"Sitting in cash by design — {d['n_open']} open position(s) "
                f"(${d['open_stake']:.0f}). Nothing has closed yet, so there's "
                f"nothing to score. That's the brake working, not a bug.") \
            if d["n_open"] else "No trades yet."
        return f"📊 STATS · {d['label']}\n{tail}"

    wr = d["wins"] / d["closed"] * 100
    losses = d["closed"] - d["wins"]
    aw = f"{d['avg_win']*100:+.2f}%" if d["avg_win"] is not None else "—"
    al = f"{d['avg_loss']*100:+.2f}%" if d["avg_loss"] is not None else "—"
    out = [f"📊 STATS · {d['label']}",
           f"Closed: {d['closed']} · Win rate: {wr:.0f}% ({d['wins']}W/{losses}L)"
           + (f" · {d['shorts']} shorts" if d["shorts"] else ""),
           f"Net P&L: {money(d['net_abs'])}"
           + (f"  ·  avg/trade {d['avg_ratio']*100:+.2f}%" if d["avg_ratio"] is not None else ""),
           f"Avg win {aw} · avg loss {al} · profit factor {_pf(d['gross_win'], d['gross_loss'])}",
           f"Best {d['best']*100:+.1f}% · Worst {d['worst']*100:+.1f}% · hold {fmt_dur(d['avg_hrs'])}"]

    # Fee drag — gross (before fees) vs net (after). The faster a bot trades, the more
    # the exchange skims; this line is why the scalp/day-trade lab bots exist.
    gross = d["net_abs"] + d["fees"]
    fee_line = f"Fee drag: {money(gross)} gross → {money(d['net_abs'])} net · ${d['fees']:.2f} fees"
    if d["gross_win"] > 0:
        fee_line += f" (ate {d['fees'] / d['gross_win'] * 100:.0f}% of gross winnings)"
    if abs(d["funding"]) > 1e-9:
        fee_line += f" · funding ${d['funding']:+.2f}"
    out.append(fee_line)

    if d["reasons"]:
        out.append("\nWhere it ended (exit reason):")
        for r in d["reasons"]:
            rwr = r["wins"] / r["n"] * 100 if r["n"] else 0
            avg = f" avg {r['avg_ratio']*100:+.1f}%" if r["avg_ratio"] is not None else ""
            out.append(f"  {r['reason']}: {r['n']}× · {rwr:.0f}%W · {money(r['net'])}{avg}")

    winners = [p for p in d["pairs"] if p["net"] > 0][:3]
    losers = [p for p in reversed(d["pairs"]) if p["net"] < 0][:3]
    if winners or losers:
        out.append("\nBy coin:")
        if winners:
            out.append("  🟢 " + " · ".join(f"{p['pair'].split('/')[0]} +${p['net']:.2f}" for p in winners))
        if losers:
            out.append("  🔴 " + " · ".join(f"{p['pair'].split('/')[0]} -${abs(p['net']):.2f}" for p in losers))

    if d["n_open"]:
        out.append(f"\n{d['n_open']} still open (${d['open_stake']:.0f} staked)")
    out.append("\n— paper trading · SQL over the live trade DB · not financial advice")
    return "\n".join(out)


def all_summary():
    blocks = [summary_line(bot_stats(k)) for k in BOTS]
    return ("📊 TRADE STATS (paper)\n\n" + "\n".join(blocks) +
            "\n\nSend /stats spot|futures|brakedhold|scalp|daytrade for the full "
            "breakdown (exit reasons, per-coin, fee drag).")


def stats(args=None):
    """Entry point for the Telegram /stats command."""
    if args:
        key = args[0].lower()
        alias = {"braked": "brakedhold", "hold": "brakedhold", "fut": "futures"}
        key = alias.get(key, key)
        if key not in BOTS:
            return ("❌ Unknown bot. Use: /stats spot | futures | brakedhold\n"
                    "(or just /stats for all three)")
        return detail(bot_stats(key))
    return all_summary()


if __name__ == "__main__":
    import sys
    print(stats(sys.argv[1:]))
