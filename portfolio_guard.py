#!/usr/bin/env python3
"""
Portfolio Guardian — the 'shared brain' coordinating the two independent bots.
Watches BOTH and enforces portfolio-level rules via their REST APIs. Priority order:

  RULE 0 (CIRCUIT BREAKER, top priority): if combined balance falls > MAX_DRAWDOWN
     from its peak (high-water mark), TRIP — close ALL positions on both bots and
     HALT trading. Does NOT auto-resume (a big loss needs human review) — send
     /reset in Telegram to clear it.
  RULE 1 (exposure caps): combined open trades ≤ MAX_TOTAL_OPEN AND $ ≤ MAX_TOTAL_STAKE,
     else pause both; resume when clear.
  RULE 2 (concentration): both LONG same coin -> auto-close the futures duplicate
     (hedges left alone; per-coin cooldown to avoid fighting the strategy).

State persisted in guard_state.json; re-read each poll so /reset takes effect.
"""
import time, os
from local_secrets import api_pw

import freqtrade_api
import state_io
from freqtrade_api import get as api_get
from state_io import save_json, telegram_conf, verified_send

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
CHAT, API = telegram_conf(CONF)
STATE = "/Users/vikasreddy/cryptobot/guard_state.json"
# One-shot flag written by TraderJoy's /reset. The guardian is the SINGLE WRITER of
# guard_state.json — /reset used to write it directly and could be silently clobbered
# by this loop's read-modify-write (audit finding). Now /reset just drops this flag
# and the guardian consumes it at the top of its cycle.
RESET_REQ = "/Users/vikasreddy/cryptobot/guard_reset_request.json"

SPOT = ("Spot", 8080, api_pw(8080))
FUTURES = ("Futures", 8081, api_pw(8081))
BOTS = [SPOT, FUTURES]

POLL = 15                 # seconds between portfolio checks
MAX_TOTAL_OPEN = 4        # cap on NUMBER of combined open trades
MAX_TOTAL_STAKE = 400     # cap on total $ at risk across both bots
MAX_DRAWDOWN = 0.10       # circuit breaker: trip if combined balance drops this % from peak
CONC_COOLDOWN = 3600      # seconds before acting on the same coin again


def send(text):
    """Verified send — True only if Telegram confirmed delivery."""
    return verified_send(API, CHAT, text, timeout=15, feed_source="guardian")


def api_post(port, pw, ep, body=None):
    """log=True: a guardian command that fails silently is how a breach goes unenforced."""
    return freqtrade_api.post(port, pw, ep, body, log=True)


def coin(pair):
    return pair.split("/")[0]


def load_state():
    # strict: a corrupt guard_state.json must NEVER read as {} — that silently
    # un-trips the circuit breaker and re-baselines the high-water mark. Raise so the
    # loop below alerts and skips the cycle (taking NO trading action) rather than
    # acting on a blank slate. A missing file (true first boot) still returns {}.
    if not os.path.exists(STATE):
        return {}
    return state_io.load_json(STATE, {}, strict=True)


def main():
    send(f"🧠 Portfolio Guardian online.\n"
         f"• 🚨 Circuit breaker: halt all if drawdown > {MAX_DRAWDOWN*100:.0f}% from peak (manual /reset)\n"
         f"• Combined ≤ {MAX_TOTAL_OPEN} trades AND ≤ ${MAX_TOTAL_STAKE} exposure (else pause both)\n"
         f"• Both LONG same coin → auto-close futures duplicate (hedges left alone)")

    dd_breach_count = 0        # RULE 0 requires the breach on 2 CONSECUTIVE polls
    while True:
        try:
            # consume a /reset request (flag file written by TraderJoy) — this loop
            # is the single writer of guard_state.json, so no read-modify-write race
            try:
                state = load_state()
            except state_io.StateCorrupt as e:
                # blind is safer than wrong: don't act on a blank breaker state
                send(f"🚨 GUARDIAN: guard_state.json unreadable ({e}); skipping this "
                     f"cycle and taking NO action. Fix/delete the file to resume.")
                dd_breach_count = 0
                time.sleep(POLL)
                continue
            if os.path.exists(RESET_REQ):
                try:
                    os.remove(RESET_REQ)
                except Exception:
                    pass
                state["breaker_tripped"] = False
                state["paused_by_guard"] = False
                state["peak_balance"] = 0.0    # re-baseline high-water mark
                dd_breach_count = 0
                if not save_json(STATE, state):
                    send("⚠️ GUARDIAN: /reset could not be persisted (disk?) — it may "
                         "re-apply next cycle. Check disk/permissions.")
                for _, port, pw in BOTS:
                    api_post(port, pw, "reload_config")
                send("🔄 GUARDIAN: /reset applied — breaker cleared, peak re-baselined, "
                     "both bots resumed.")
            paused = state.get("paused_by_guard", False)
            cooldown = state.get("cooldown", {})
            peak = state.get("peak_balance", 0.0)
            tripped = state.get("breaker_tripped", False)

            statuses, balances, offline = {}, {}, False
            for name, port, pw in BOTS:
                s = api_get(port, pw, "status")
                b = api_get(port, pw, "balance")
                tot = (b or {}).get("total") if isinstance(b, dict) else None
                # a JSON error body ({"detail":...}/{"error":...}) parses fine but has
                # no numeric total — treating it as $0 once false-tripped the breaker
                # into force-closing everything (audit HIGH). Error body == offline.
                if not isinstance(s, list) or not isinstance(tot, (int, float)) \
                        or isinstance(tot, bool):
                    offline = True
                statuses[name] = s if isinstance(s, list) else []
                balances[name] = tot if isinstance(tot, (int, float)) else 0
            if offline:
                dd_breach_count = 0
                time.sleep(POLL)
                continue

            combined_bal = sum(balances.values())
            if combined_bal > peak:
                peak = combined_bal
            dd = (peak - combined_bal) / peak if peak > 0 else 0.0
            total_open = sum(len(v) for v in statuses.values())
            total_stake = sum(t.get("stake_amount", 0) for v in statuses.values() for t in v)

            if tripped:
                # halted — keep re-asserting stopentry in case a bot restarted
                for _, port, pw in BOTS:
                    api_post(port, pw, "stopentry")

            # ---- RULE 0: CIRCUIT BREAKER (top priority) ----
            # requires the breach on 2 consecutive polls so a single bad read
            # (glitchy balance, reload window) can never force-close everything
            elif dd >= MAX_DRAWDOWN and dd_breach_count < 1:
                dd_breach_count += 1
                print(f"drawdown breach #{dd_breach_count} ({dd*100:.1f}%) — "
                      f"confirming next poll before tripping", flush=True)

            elif dd >= MAX_DRAWDOWN:
                # don't CLAIM "closed all" if the API calls failed — a swallowed
                # forceexit error here would leave positions open while the alert
                # says they're flat, the most dangerous possible false report
                failed = []
                for nm, port, pw in BOTS:
                    fx = api_post(port, pw, "forceexit", {"tradeid": "all"})
                    if not isinstance(fx, dict) or fx.get("error"):
                        failed.append(nm)
                    api_post(port, pw, "stopentry")
                tripped = True
                paused = True
                dd_breach_count = 0
                closed_line = ("CLOSED ALL positions and HALTED both bots."
                               if not failed else
                               f"HALTED both bots, but the force-close FAILED on: "
                               f"{', '.join(failed)} — positions may still be OPEN. "
                               f"CHECK MANUALLY NOW.")
                send(f"🚨🚨 CIRCUIT BREAKER TRIPPED — portfolio drawdown {dd*100:.1f}% "
                     f"(peak ${peak:.0f} → ${combined_bal:.0f}), confirmed on 2 polls. "
                     f"{closed_line} Manual review required — send /reset to resume.")

            else:
                dd_breach_count = 0            # breach didn't persist — stand down

                # ---- RULE 1: exposure caps (count and $) ----
                over_count = total_open >= MAX_TOTAL_OPEN
                over_stake = total_stake >= MAX_TOTAL_STAKE
                breach = over_count or over_stake
                if breach and not paused:
                    reasons = []
                    if over_count:
                        reasons.append(f"{total_open} trades ≥ {MAX_TOTAL_OPEN}")
                    if over_stake:
                        reasons.append(f"${total_stake:.0f} ≥ ${MAX_TOTAL_STAKE} exposure")
                    for _, port, pw in BOTS:
                        api_post(port, pw, "stopentry")
                    paused = True
                    send("🛑 GUARDIAN: portfolio limit hit (" + " + ".join(reasons) +
                         "). Paused new entries on both bots. Auto-resumes when it clears.")
                elif not breach and paused:
                    for _, port, pw in BOTS:
                        api_post(port, pw, "reload_config")
                    paused = False
                    send(f"▶️ GUARDIAN: back under limits ({total_open} trades, "
                         f"${total_stake:.0f} exposure). Resumed entries on both bots.")

                # ---- RULE 2: concentration -> auto-close futures duplicate ----
                spot_longs = {coin(t["pair"]) for t in statuses["Spot"] if not t.get("is_short")}
                fut_trades = statuses["Futures"]
                fut_longs = {coin(t["pair"]) for t in fut_trades if not t.get("is_short")}
                concentrated = spot_longs & fut_longs
                now = time.time()
                for c in sorted(concentrated):
                    if now - cooldown.get(c, 0) < CONC_COOLDOWN:
                        continue
                    ft = next((t for t in fut_trades
                               if coin(t["pair"]) == c and not t.get("is_short")
                               and (t.get("amount") or 0) > 0), None)
                    if not ft:
                        continue
                    r = api_post(FUTURES[1], FUTURES[2], "forceexit", {"tradeid": str(ft["trade_id"])})
                    if r.get("error"):
                        print("forceexit failed, will retry:", r["error"], flush=True)
                        continue
                    cooldown[c] = now
                    send(f"⚠️➡️✅ GUARDIAN: both bots were LONG {c} (concentration). "
                         f"Auto-closed the futures leg #{ft['trade_id']}. Kept the spot position.")

            if not save_json(STATE, {"paused_by_guard": paused, "cooldown": cooldown,
                                     "peak_balance": peak, "breaker_tripped": tripped}):
                # a lost write can silently lose a fresh breaker-trip — make it loud
                print("GUARDIAN: guard_state.json write FAILED — state not persisted "
                      "this cycle", flush=True)
        except Exception as e:
            # reset the breach counter too — otherwise "2 CONSECUTIVE polls" would
            # degrade to "2 breach polls separated by any number of error polls"
            dd_breach_count = 0
            print("loop err", e, flush=True)
            time.sleep(5)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
