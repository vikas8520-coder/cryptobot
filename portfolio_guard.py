#!/usr/bin/env python3
"""
Portfolio Guardian — the 'shared brain' coordinating the independent Freqtrade bots.
Watches all configured bots and enforces portfolio-level rules via their REST APIs. Priority order:

  RULE 0 (CIRCUIT BREAKER, top priority): if combined balance falls > MAX_DRAWDOWN
     from its peak (high-water mark), TRIP — close ALL positions on all watched bots and
     HALT trading. Does NOT auto-resume (a big loss needs human review) — send
     /reset in Telegram to clear it.
  RULE 1 (exposure caps): combined open trades ≤ MAX_TOTAL_OPEN AND $ ≤ MAX_TOTAL_STAKE,
     else pause all watched bots; resume when clear.
  RULE 2 (concentration): REMOVED 2026-07-28 — the Spot bot was parked, so the
     Spot+Futures concentration check is no longer active.

State persisted in guard_state.json; re-read each poll so /reset takes effect.

Scope-change guard (2026-07-29): the sorted BOTS names are stored in state as
"scope". If BOTS is refactored (a bot dropped or added), the stale peak_balance
from the old scope would false-trip the breaker — the guardian detects the
change, re-baselines peak=0, clears the breaker, and announces the scope shift.
"""
import requests, re, time, os, socket, atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from local_secrets import api_pw
from requests.auth import HTTPBasicAuth

import state_io
from state_io import save_json, verified_send

# Hard global socket timeout so DNS and stalled TLS handshakes cannot block the
# guardian loop indefinitely. Per-request requests/urllib3 timeouts override this
# for open sockets, but the default protects slow resolution/connect paths.
socket.setdefaulttimeout(8)

# Telegram sends run in a background thread so a slow Telegram/DNS response cannot
# freeze the guardian main loop. The main thread waits max 8s for each send.
_send_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="guardian_send")
atexit.register(lambda: _send_pool.shutdown(wait=False))

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"
STATE = "/Users/vikasreddy/cryptobot/guard_state.json"
# One-shot flag written by TraderJoy's /reset. The guardian is the SINGLE WRITER of
# guard_state.json — /reset used to write it directly and could be silently clobbered
# by this loop's read-modify-write (audit finding). Now /reset just drops this flag
# and the guardian consumes it at the top of its cycle.
RESET_REQ = "/Users/vikasreddy/cryptobot/guard_reset_request.json"

# 2026-08-20: expanded from Futures-only to all currently-running non-OKX bots.
# OKX futures/scalp/eth are unloaded until OKX connectivity returns or we migrate
# to Gate.io; adding them here would make the guardian see one offline bot and skip
# every cycle. They can be re-added once their launchd jobs are reloaded.
BOTS = [
    ("Braked Hold", 8082, api_pw(8082)),
    ("S&P 500", 8086, api_pw(8086)),
    ("Nifty 50", 8087, api_pw(8087)),
    ("ONGC", 8088, api_pw(8088)),
    ("ITC", 8089, api_pw(8089)),
    ("BTC", 8091, api_pw(8091)),
]

POLL = 15                 # seconds between portfolio checks
MAX_TOTAL_OPEN = 12       # cap on NUMBER of combined open trades
MAX_TOTAL_STAKE = 1200    # cap on total $ at risk across all watched bots
MAX_DRAWDOWN = 0.10       # circuit breaker: trip if combined balance drops this % from peak
CONC_COOLDOWN = 3600      # seconds before acting on the same coin again
_scope_peak = {}


def send(text):
    """Verified send — True only if Telegram confirmed delivery.
    Never block the main loop: background thread with 8s wait."""
    fut = _send_pool.submit(verified_send, API, CHAT, text, timeout=8, feed_source="guardian")
    try:
        return fut.result(timeout=8)
    except TimeoutError:
        print("telegram send did not complete in 8s, continuing", flush=True)
        return False


def api_get(port, pw, ep):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/{ep}",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=8).json()
    except Exception:
        return None


def api_post(port, pw, ep, body=None):
    try:
        return requests.post(f"http://127.0.0.1:{port}/api/v1/{ep}", json=body or {},
                             auth=HTTPBasicAuth("freqtrader", pw), timeout=10).json()
    except Exception as e:
        print("post err", e, flush=True)
        return {"error": str(e)}


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
         f"• Combined ≤ {MAX_TOTAL_OPEN} trades AND ≤ ${MAX_TOTAL_STAKE} exposure (else pause all watched bots)")

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
                state["scope"] = tuple(sorted(name for name, _, _ in BOTS))
                dd_breach_count = 0
                if not save_json(STATE, state):
                    send("⚠️ GUARDIAN: /reset could not be persisted (disk?) — it may "
                         "re-apply next cycle. Check disk/permissions.")
                for _, port, pw in BOTS:
                    api_post(port, pw, "reload_config")
                send("🔄 GUARDIAN: /reset applied — breaker cleared, peak re-baselined, "
                     "all watched bots resumed.")
            paused = state.get("paused_by_guard", False)
            cooldown = state.get("cooldown", {})
            peak = state.get("peak_balance", 0.0)
            tripped = state.get("breaker_tripped", False)

            # 2026-07-29: scope-change guard. When BOTS is refactored (a bot is
            # dropped or added), the stale peak_balance from the old scope can
            # false-trip the breaker — this happened when Spot was parked and the
            # guardian went from 2-bot to 1-bot scope: peak $1987 vs new $1000 =
            # 49.7% "drawdown" that was really just a smaller watch list. Store
            # the sorted bot names in state; if they change, re-baseline peak=0
            # and clear the breaker so the next poll sets a fresh high-water mark.
            scope_key = tuple(sorted(name for name, _, _ in BOTS))
            prev_scope = tuple(state.get("scope", []))
            was_paused = paused
            if prev_scope != scope_key:
                if scope_key in _scope_peak:
                    peak = _scope_peak[scope_key]
                else:
                    if prev_scope:
                        send(f"ℹ️ GUARDIAN: watch scope changed ({', '.join(prev_scope)} → "
                             f"{', '.join(scope_key)}). Re-baselining peak and clearing breaker "
                             f"to avoid a false drawdown trip from the old scope's high-water mark.")
                    peak = 0.0
                    tripped = False
                    paused = False
                    dd_breach_count = 0
                    if was_paused:
                        for _, port, pw in BOTS:
                            api_post(port, pw, "reload_config")
                        send("▶️ GUARDIAN: scope re-baseline resumed entries on all bots.")

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
                closed_line = ("CLOSED ALL positions and HALTED all watched bots."
                               if not failed else
                               f"HALTED all watched bots, but the force-close FAILED on: "
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
                         "). Paused new entries on all watched bots. Auto-resumes when it clears.")
                elif not breach and paused:
                    for _, port, pw in BOTS:
                        api_post(port, pw, "reload_config")
                    paused = False
                    send(f"▶️ GUARDIAN: back under limits ({total_open} trades, "
                         f"${total_stake:.0f} exposure). Resumed entries on all watched bots.")

                # ---- RULE 2: REMOVED 2026-07-28 ----
                # Was: concentration check (Spot+Futures both long same coin).
                # Spot bot parked (edge expired 2024), so the rule is dead code.
                # The cooldown dict is kept in state for backward compat but no
                # longer populated.

            _scope_peak[scope_key] = peak
            if not save_json(STATE, {"paused_by_guard": paused, "cooldown": cooldown,
                                     "peak_balance": peak, "breaker_tripped": tripped,
                                     "scope": scope_key}):
                # a lost write can silently lose a fresh breaker-trip — make it loud
                print("GUARDIAN: guard_state.json write FAILED — state not persisted "
                      "this cycle", flush=True)
        except Exception:
            # reset the breach counter too — otherwise "2 CONSECUTIVE polls" would
            # degrade to "2 breach polls separated by any number of error polls"
            dd_breach_count = 0
            import traceback
            print("loop err", traceback.format_exc(), flush=True)
            time.sleep(5)
        time.sleep(POLL)


if __name__ == "__main__":
    main()
