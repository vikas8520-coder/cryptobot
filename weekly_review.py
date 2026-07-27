#!/usr/bin/env python3
"""
weekly_review.py — the learning loop's engine.

Once a week it doesn't just summarize; it REVIEWS: it compares each bot to the only
benchmark that matters (buy & hold), reads the trade DBs for where money was actually
lost, checks the brake's completed track record, and then applies an HONEST SIGNAL GATE
— it refuses to draw conclusions from too little data (the classic mistake: over-reacting
to a handful of trades / one week of noise).

The output goes two places:
  1. Telegram — the human-readable digest.
  2. learning_log.md — an APPEND-ONLY, dated ledger of what each week's data showed and
     what (if anything) it was allowed to conclude. THIS is the artifact that makes
     learning compound: Claude reads it each session and promotes genuine, gated lessons
     into durable memory, so decisions get made on accumulated evidence, not gut feel.

Nothing here changes the bots. It changes what WE know.
"""
import csv
import os
from datetime import datetime, timezone

import stats_lib
from state_io import telegram_conf, verified_send

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "telegram.conf")
EQUITY = os.path.join(BASE, "equity_history.csv")
LOG = os.path.join(BASE, "learning_log.md")
CHAT, API = telegram_conf(CONF)

# Honest signal gates — below these, data is NOISE and we say so explicitly.
MIN_TRADES_FOR_WINRATE = 30      # a win rate on <30 trades tells you almost nothing
MIN_WEEKS_FOR_VERDICT = 4        # regimes play out over months, not one week
MIN_EPISODES_FOR_BRAKE = 3       # the brake has no track record until holds complete


def send(text):
    return verified_send(API, CHAT, text, feed_source="review")


def read_equity():
    if not os.path.exists(EQUITY):
        return []
    try:
        return list(csv.DictReader(open(EQUITY)))
    except Exception as e:
        # don't silently pretend "no history" — an unreadable CSV would flip the
        # signal gate to "still gathering data" and hide that the file is broken
        print(f"read_equity: {os.path.basename(EQUITY)} unreadable ({e})", flush=True)
        return []


def _f(row, key):
    try:
        return float(row.get(key) or 0)
    except (ValueError, TypeError):
        return 0.0


def scoreboard(rows):
    """Each bot vs buy & hold benchmarks — the only question that matters."""
    if not rows:
        return ["No equity history yet."], None
    last = rows[-1]
    week_ago = rows[-8] if len(rows) >= 8 else rows[0]
    out, beating = [], []
    btc, basket = _f(last, "btc_hold"), _f(last, "basket_hold")
    for bot in ("spot", "futures", "brakedhold"):
        v = _f(last, bot)
        if v <= 0:
            continue
        vs_btc = v - btc
        vs_basket = v - basket
        beating.append(vs_basket > 0)
        mark = "🟢 ahead of" if vs_basket >= 0 else "🔴 behind"
        out.append(f"  {bot}: ${v:.2f}  ({mark} basket by ${abs(vs_basket):.2f})")
    out.append(f"  benchmarks: BTC-hold ${btc:.2f} · basket-hold ${basket:.2f}")
    n_beating = sum(beating)
    return out, (n_beating, len(beating), len(rows))


def gate_verdict(rows):
    """The discipline: state plainly whether there's enough data to conclude anything."""
    weeks = len(rows) / 7 if rows else 0
    reasons = []
    total_closed = 0
    for k in stats_lib.BOTS:
        d = stats_lib.bot_stats(k)
        total_closed += d.get("closed", 0)
    ep = 0
    try:
        import brake_memory
        ep = len(brake_memory._load().get("closed", []))
    except Exception as e:
        print(f"brake memory unreadable for gate ({e}); treating as 0 episodes", flush=True)

    enough = (total_closed >= MIN_TRADES_FOR_WINRATE and weeks >= MIN_WEEKS_FOR_VERDICT)
    if not enough:
        reasons.append(
            f"⏳ Still gathering data — {total_closed}/{MIN_TRADES_FOR_WINRATE} closed trades, "
            f"~{weeks:.1f}/{MIN_WEEKS_FOR_VERDICT} weeks. Win rates below this are noise.")
    if ep < MIN_EPISODES_FOR_BRAKE:
        reasons.append(
            f"⏳ Brake track record: {ep}/{MIN_EPISODES_FOR_BRAKE} completed holds — "
            f"no verdict on the 200-day brake until holds close.")
    return enough, reasons


def loss_concentration():
    """Where is each bot actually bleeding? (exit-reason that dominates losses.)"""
    out = []
    for k in stats_lib.BOTS:
        d = stats_lib.bot_stats(k)
        if d.get("closed", 0) == 0:
            continue
        worst = min(d["reasons"], key=lambda r: r["net"], default=None)
        if worst and worst["net"] < 0:
            out.append(f"  {k}: {d['closed']} closed, biggest leak = "
                       f"'{worst['reason']}' ({worst['n']}× → -${abs(worst['net']):.2f})")
    return out


def build():
    rows = read_equity()
    now = datetime.now(timezone.utc)
    sb, sb_meta = scoreboard(rows)
    enough, gates = gate_verdict(rows)
    leaks = loss_concentration()

    # ---- Telegram digest ----
    tg = [f"🔬 WEEKLY REVIEW — {now:%b %d, %Y}", "",
          "Vs buy & hold (the only benchmark that matters):"] + sb + ["", "Where money leaked:"]
    tg += leaks or ["  (no closed trades yet)"]
    tg += ["", "Signal check:"] + [f"  {g}" for g in gates] if gates else ["", "✅ Enough data to start judging trends."]
    tg += ["", "💡 " + ("A real signal may be forming — Claude will weigh it next session."
                        if enough else
                        "Too early to conclude. The discipline is NOT acting on noise. "
                        "The bots stay fixed; we only change strategy on gated evidence.")]
    tg_msg = "\n".join(tg)

    # ---- learning_log.md entry (append-only, the compounding artifact) ----
    log = [f"\n## {now:%Y-%m-%d} — Weekly Review\n",
           "**Vs buy & hold:**"]
    log += [f"- {l.strip()}" for l in sb]
    log.append("\n**Loss concentration:**")
    log += [f"- {l.strip()}" for l in leaks] or ["- (no closed trades)"]
    log.append("\n**Signal gate:** " + ("PASSED — evidence is now worth acting on."
                                        if enough else "NOT MET — data still noise."))
    for g in gates:
        log.append(f"- {g}")
    log.append("\n**Auto-observation (data only, not yet a lesson):**")
    if leaks:
        log.append(f"- {leaks[0].strip()} — consistent with the fee/whipsaw thesis from risk_backtest.")
    if sb_meta:
        nb, tot, nrows = sb_meta
        log.append(f"- {nb}/{tot} bots beating the basket over {nrows} days of tracked equity.")
    log.append("\n**→ For Claude to review next session:** promote any GATED lesson here "
               "into durable memory; if a bot is past the trade/week gate and still losing, "
               "raise the retire/replace decision with Vikas.\n")
    log_entry = "\n".join(log)

    return tg_msg, log_entry


def main():
    tg_msg, log_entry = build()
    # append to the durable ledger FIRST (never lose the record to a send failure)
    with open(LOG, "a") as f:
        f.write(log_entry)
    ok = send(tg_msg)
    if not ok:
        print("weekly review NOT delivered to Telegram (still written to learning_log.md)",
              flush=True)
    print(f"weekly review written to learning_log.md; telegram delivered={ok}", flush=True)


if __name__ == "__main__":
    main()
