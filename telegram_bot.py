#!/usr/bin/env python3
"""
TraderJoy command center — a single Telegram bot to monitor & control BOTH
trading bots (spot + futures) and the funding-carry monitor from your phone.

Long-polls Telegram; only responds to YOUR chat id. One process only (one bot
can be polled by one listener). Proxies to each bot's Freqtrade REST API.

Read commands:  /status /profit /balance /carry /summary /help
Action commands: /pause <bot>   /resume <bot>   /close <bot> <id|all>
"""
import requests, subprocess, re, time, os, json
from local_secrets import api_pw
from requests.auth import HTTPBasicAuth

from state_io import save_json, verified_send

GUARD_STATE = "/Users/vikasreddy/cryptobot/guard_state.json"
GUARD_RESET_REQ = "/Users/vikasreddy/cryptobot/guard_reset_request.json"

CONF = "/Users/vikasreddy/cryptobot/telegram.conf"
_c = open(CONF).read()
TOK = re.search(r'TG_TOKEN="([^"]+)"', _c).group(1)
CHAT = str(re.search(r'TG_CHAT="([^"]+)"', _c).group(1))
API = f"https://api.telegram.org/bot{TOK}"
PYBIN = "/Users/vikasreddy/cryptobot/.venv/bin/python3"
MONITOR = "/Users/vikasreddy/cryptobot/funding_monitor.py"

BOTS = [("Spot", 8080, api_pw(8080)), ("Futures", 8081, api_pw(8081)),
        ("ApeX", 8085, api_pw(8085)), ("S&P 500", 8086, api_pw(8086)),
        ("Nifty 50", 8087, api_pw(8087)),
        ("ONGC", 8088, api_pw(8088)), ("ITC", 8089, api_pw(8089))]
BOTS_BY_NAME = {"spot": (8080, api_pw(8080)), "futures": (8081, api_pw(8081)),
                "apex": (8085, api_pw(8085)), "spx": (8086, api_pw(8086)),
                "nifty": (8087, api_pw(8087)), "ongc": (8088, api_pw(8088)),
                "itc": (8089, api_pw(8089))}
# The help/menu strings below list these names verbatim; a bot added here and NOT there is
# a bot the user can never reach from Telegram.
BOT_NAMES = "|".join(BOTS_BY_NAME)


def redact(s):
    """Never let the bot token reach a log line (it leaked once — audit finding)."""
    return str(s).replace(TOK, "<token>")


def send(text):
    """Verified, 4096-chunked send. Returns True only on confirmed delivery."""
    return verified_send(API, CHAT, text, timeout=15)


def api_get(port, pw, ep):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/{ep}",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=6).json()
    except Exception:
        return None


def api_post(port, pw, ep, body=None):
    try:
        return requests.post(f"http://127.0.0.1:{port}/api/v1/{ep}", json=body or {},
                             auth=HTTPBasicAuth("freqtrader", pw), timeout=10).json()
    except Exception as e:
        return {"error": str(e)}


def cmd_status(args):
    out = ["📊 OPEN TRADES"]
    for name, port, pw in BOTS:
        st = api_get(port, pw, "status")
        if st is None:
            out.append(f"• {name}: offline")
        elif not st:
            out.append(f"• {name}: no open trades")
        else:
            out.append(f"• {name}:")
            for t in st:
                d = "SHORT" if t.get("is_short") else "LONG"
                pr = t.get("profit_ratio") or 0    # null on a just-opened trade
                out.append(f"    #{t['trade_id']} {t['pair']} {d}  {pr*100:+.2f}%")
    return "\n".join(out)


def cmd_profit(args):
    out = ["💰 PROFIT (paper)"]
    for name, port, pw in BOTS:
        p = api_get(port, pw, "profit")
        out.append(f"• {name}: {p.get('profit_closed_percent',0):+.2f}%  "
                   f"({p.get('closed_trade_count',0)} trades, win {p.get('winrate',0)*100:.0f}%)"
                   if p else f"• {name}: offline")
    return "\n".join(out)


def cmd_balance(args):
    out = ["🏦 WALLET (paper)"]
    for name, port, pw in BOTS:
        b = api_get(port, pw, "balance")
        out.append(f"• {name}: ${b.get('total',0):.2f}" if b else f"• {name}: offline")
    return "\n".join(out)


def cmd_carry(args):
    try:
        r = subprocess.run([PYBIN, MONITOR], capture_output=True, text=True, timeout=60)
        keep = [l.strip() for l in r.stdout.splitlines()
                if any(k in l for k in ["AVG", "STRONG", "MODEST", "SKIP"])]
        return "📈 FUNDING CARRY\n" + "\n".join(keep) if keep else "carry check failed"
    except Exception as e:
        return f"carry error: {e}"


def cmd_summary(args):
    return cmd_profit([]) + "\n\n" + cmd_status([]) + "\n\n" + cmd_carry([])


def cmd_brake(args):
    """On-demand 200-day brake status for the personal watchlist — is each coin
    above its line (hold) or below (cash)? Computes live from a reachable exchange."""
    try:
        import brake_alerts as ba
    except Exception as e:
        return f"❌ brake module error: {e}"
    watch = ba.load_json(ba.WATCHLIST, {"coins": ba.DEFAULT_WATCH}).get("coins", ba.DEFAULT_WATCH)
    src, ex = ba.pick_exchange()
    if ex is None:
        return "❌ No reachable exchange right now — try again shortly."
    lines, hold, cash = [], 0, 0
    for coin in watch:
        try:
            st, price, sma = ba.brake_state(ex, coin)
        except Exception as e:
            lines.append(f"• {coin}: error ({str(e)[:32]})")
            continue
        gap = (price / sma - 1) * 100
        if st == "above":
            hold += 1
            lines.append(f"🟢 HOLD  {coin}  {ba.fmt(price)}  ({gap:+.0f}% vs 200d)")
        else:
            cash += 1
            lines.append(f"🔴 CASH  {coin}  {ba.fmt(price)}  ({gap:+.0f}% vs 200d)")
    tail = f"\n{hold} holding · {cash} in cash"
    if hold == 0 and cash > 0:
        tail += "\nBrake fully engaged — every asset is below its 200-day line (downtrend). Sit tight in cash."
    elif cash == 0 and hold > 0:
        tail += "\nAll clear — every asset above its line. Holding."
    return f"🪂 BRAKE STATUS — live (source: {src})\n" + "\n".join(lines) + tail


def cmd_memory(args):
    """The bots' journal: how the brake's holds have actually turned out over time."""
    try:
        import brake_memory
    except Exception as e:
        return f"❌ memory error: {e}"
    prices = {}
    sf = "/Users/vikasreddy/cryptobot/brake_alert_state.json"
    if os.path.exists(sf):
        try:
            prices = {c: d.get("price") for c, d in json.load(open(sf)).items()}
        except Exception:
            pass
    return brake_memory.summary(current_prices=prices)


def _paused_note(job, board_file, subject, detail):
    """Footer shown when a scheduled monitor is disabled (simplified 2026-07-20).
    The command's own output is always LIVE — this only flags that AUTO-alerting is off
    and the dashboard panel is frozen. Gated on the plist location, so it self-clears the
    moment the job is re-enabled — no hardcoded flag to forget."""
    active = os.path.expanduser(f"~/Library/LaunchAgents/com.vikas.{job}.plist")
    if os.path.exists(active):
        return ""                       # monitor is running — nothing to warn about
    base = os.path.dirname(os.path.abspath(__file__))
    when = "?"
    try:
        b = json.load(open(os.path.join(base, board_file)))
        ts = b.get("ts") or b.get("updated") or ""      # divbrake uses ts, trendline uses updated
        when = ts[:16].replace("T", " ") + " UTC" if ts else "?"
    except Exception:
        pass
    return (f"\n\n⏸ Auto-monitor PAUSED (simplified 2026-07-20). The {subject} above is LIVE "
            f"(just computed) — but {detail}, and the dashboard panel is frozen at {when}. "
            f"Re-enable: {job} in disabled_plists/.")


def cmd_portfolio(args):
    """The diversified braked portfolio map — how much the 200-day brake says to hold
    right now across all four sleeves (crypto / gold / bonds / stocks), and the overall
    deployed-vs-cash split. Computes live."""
    try:
        import diversified_brake as d
        m = d.build_map()
        if not m:
            return "❌ Not enough data right now — try again shortly."
        return d.portfolio_text(m) + _paused_note(
            "divbrake", "diversified_brake_board.json", "map",
            "you're no longer auto-pinged when the allocation changes")
    except Exception as e:
        return f"❌ portfolio error: {type(e).__name__}: {e}"


def cmd_review(args):
    """Live weekly-style review: each bot vs buy & hold, where money leaked, and an
    honest signal-gate verdict (refuses to conclude on noise). The scheduled Sunday
    job runs this same review and appends it to learning_log.md."""
    try:
        import weekly_review
        tg, _ = weekly_review.build()
        return tg
    except Exception as e:
        return f"❌ review error: {type(e).__name__}: {e}"


def cmd_stats(args):
    """Trade-analysis scoreboard straight from each bot's SQLite trade DB (read-only):
    win rate, net P&L, profit factor, avg hold, fees, and — the useful part — the
    breakdown BY EXIT REASON and per coin. /stats = all bots; /stats <bot> = detail."""
    try:
        import stats_lib
        return stats_lib.stats(args)
    except Exception as e:
        return f"❌ stats error: {type(e).__name__}: {e}"


def cmd_trend(args):
    """On-demand trendline scan — Tori Trades' 'valid line' filters, coded. For each
    coin: is it bouncing off a valid support, rejected at resistance, breaking out /
    down, or just sitting inside its channel (no valid setup)? Computes live."""
    try:
        import trendline_signal as ts
    except Exception as e:
        return f"❌ trendline module error: {e}"
    coins = ts.load_json(ts.WATCHLIST, {"coins": ts.DEFAULT_WATCH}).get("coins", ts.DEFAULT_WATCH)
    src, ex = ts.pick_exchange()
    if ex is None:
        return "❌ No reachable exchange right now — try again shortly."
    icon = {"BOUNCE_LONG": "🟢", "BREAK_UP": "🚀", "BREAK_DOWN": "🔴", "REJECT_SHORT": "⚠️"}
    setups, quiet = [], 0
    for coin in coins:
        try:
            r = ts.evaluate(coin, ex)
        except Exception:
            continue
        sig = r["signals"][0] if r["signals"] else None
        if not sig:
            quiet += 1
            continue
        setups.append(f"{icon.get(sig['type'],'•')} {coin}: {sig['type'].replace('_',' ').lower()} "
                      f"— {ts.fmt(sig['entry'])} → target {ts.fmt(sig['target'])} "
                      f"(stop {ts.fmt(sig['stop'])}, {sig['n_touches']} touches)")
    paused = _paused_note("trendline", "trendline_board.json", "scan",
                          "you're no longer auto-pinged on new breakouts")
    head = f"📐 TRENDLINE SCAN — live ({src}, {ts.TIMEFRAME})"
    if not setups:
        return (f"{head}\nNo valid setups right now — all {quiet} coins inside their channel.\n"
                f"(That's the filters working, not a miss.){paused}")
    tail = f"\n{len(setups)} setup(s) · {quiet} inside channel · not financial advice"
    return head + "\n" + "\n".join(setups) + tail + paused


def cmd_macro(args):
    """200-day brake status for the macro watchlist (stocks/bonds/gold). Reads the
    cached state the 6h macro job maintains — instant."""
    sf = "/Users/vikasreddy/cryptobot/macro_alert_state.json"
    wf = "/Users/vikasreddy/cryptobot/macro_watchlist.json"
    if not os.path.exists(sf):
        return "📈 No macro data yet — the 6h job will populate it shortly."
    try:
        state = json.load(open(sf))
    except Exception as e:
        return f"❌ macro read error: {e}"
    names = {}
    try:
        for a in json.load(open(wf)).get("assets", []):
            names[a["symbol"]] = a["name"]
    except Exception:
        pass
    lines, hold, cash = ["📈 MACRO BRAKE — stocks / bonds / gold"], 0, 0
    for sym, d in state.items():
        gap = (d["price"] / d["sma"] - 1) * 100 if d.get("sma") else 0
        if d.get("state") == "above":
            hold += 1
            lines.append(f"🟢 HOLD  {names.get(sym, sym)}  ({gap:+.0f}% vs 200d)")
        else:
            cash += 1
            lines.append(f"🔴 CASH  {names.get(sym, sym)}  ({gap:+.0f}% vs 200d)")
    lines.append(f"\n{hold} holding · {cash} in cash  ·  act via your broker")
    return "\n".join(lines)


def _resolve_bot(args):
    """Return (name, port, pw) from first arg, or None + error string."""
    if not args:
        return None, f"Which bot? Use `{'`, `'.join(BOTS_BY_NAME)}`."
    name = args[0].lower()
    if name not in BOTS_BY_NAME:
        return None, f"Unknown bot '{args[0]}'. Use `{'`, `'.join(BOTS_BY_NAME)}`."
    port, pw = BOTS_BY_NAME[name]
    return (name, port, pw), None


def cmd_close(args):
    bot, err = _resolve_bot(args)
    if err:
        return "❌ " + err + "\nUsage: /close <spot|futures|apex> <trade_id|all>"
    name, port, pw = bot
    if len(args) < 2:
        return "❌ Which trade? Usage: /close " + name + " <trade_id|all>"
    tid = args[1].lower()
    r = api_post(port, pw, "forceexit", {"tradeid": tid})
    if r.get("error"):
        return f"❌ {name}: {r['error']}"
    if r.get("result"):
        return f"✅ {name}: close requested for trade '{tid}'.\n{r['result']}"
    return f"⚠️ {name}: no trade '{tid}' to close (already closed?)."


def cmd_pause(args):
    bot, err = _resolve_bot(args)
    if err:
        return "❌ " + err + "\nUsage: /pause <spot|futures|apex>"
    name, port, pw = bot
    r = api_post(port, pw, "stopentry")
    # never claim success on a failed call (audit: /pause lied when the bot was down)
    if not isinstance(r, dict) or r.get("error"):
        detail = r.get("error", "no/invalid response") if isinstance(r, dict) else "no response"
        return f"❌ {name}: NOT paused — API call failed ({detail}). Is the bot running?"
    return (f"⏸ {name}: PAUSED — no new trades will open. Existing trades keep "
            f"being managed. Use /resume {name} to re-enable.")


def cmd_resume(args):
    bot, err = _resolve_bot(args)
    if err:
        return "❌ " + err + "\nUsage: /resume <spot|futures|apex>"
    name, port, pw = bot
    r = api_post(port, pw, "reload_config")
    if not isinstance(r, dict) or r.get("error"):
        detail = r.get("error", "no/invalid response") if isinstance(r, dict) else "no response"
        return f"❌ {name}: NOT resumed — API call failed ({detail}). Is the bot running?"
    return f"▶️ {name}: RESUMED — the bot will open new trades again."


def cmd_reset(args):
    """Request a circuit-breaker reset. The GUARDIAN applies it (single writer of
    guard_state.json) at the top of its next poll — writing the state file directly
    from here raced the guardian's read-modify-write loop and could be silently
    reverted seconds after claiming success (audit finding)."""
    if not save_json(GUARD_RESET_REQ, {"requested_at": time.time()}):
        return "❌ Could not write the reset request — check disk/permissions."
    return ("🔄 Reset REQUESTED. The Guardian applies it within ~15s and will "
            "confirm here (breaker cleared, peak re-baselined, bots resumed). "
            "If no confirmation arrives, the guardian may be down — check /help → watchdog.")


HELP = ("🤖 TraderJoy commands:\n"
        "— monitor —\n"
        "/status – open trades (both bots)\n"
        "/profit – P&L summary\n"
        "/balance – wallet balances\n"
        "/stats – trade analysis (win rate, exit reasons, per-coin) — add a bot for detail\n"
        "/review – weekly review: vs buy&hold + honest signal gate\n"
        "/carry – funding-carry status\n"
        "/brake – 200-day brake status (crypto: hold/cash per coin)\n"
        "/portfolio – diversified braked portfolio map (crypto+gold+bonds+stocks)\n"
        "/trend – trendline setups (Tori's filters: bounce/break/reject)\n"
        "/macro – brake status for stocks/bonds/gold\n"
        "/memory – the brake's track record (holds & outcomes)\n"
        "/summary – everything at once\n"
        "— control —\n"
        f"/pause <{BOT_NAMES}> – stop opening new trades\n"
        f"/resume <{BOT_NAMES}> – re-enable new trades\n"
        f"/close <{BOT_NAMES}> <id|all> – exit a trade now\n"
        "/reset – clear the circuit breaker & resume\n"
        "/help – this list")

HANDLERS = {"/status": cmd_status, "/profit": cmd_profit, "/balance": cmd_balance,
            "/stats": cmd_stats, "/review": cmd_review, "/carry": cmd_carry,
            "/brake": cmd_brake, "/portfolio": cmd_portfolio, "/trend": cmd_trend,
            "/macro": cmd_macro, "/memory": cmd_memory, "/summary": cmd_summary,
            "/pause": cmd_pause, "/resume": cmd_resume, "/close": cmd_close,
            "/reset": cmd_reset,
            "/help": lambda a: HELP, "/start": lambda a: HELP}

# Registered with Telegram so typing "/" shows a menu of commands + descriptions.
MENU = [
    ("status", "open trades (both bots)"),
    ("profit", "P&L summary"),
    ("balance", "wallet balances"),
    ("stats", "trade analysis — win rate, exit reasons, per-coin"),
    ("review", "weekly review — vs buy&hold + honest signal gate"),
    ("brake", "crypto 200-day brake status"),
    ("portfolio", "diversified braked portfolio map (crypto+gold+bonds+stocks)"),
    ("trend", "trendline setups (bounce / break / reject)"),
    ("macro", "stocks / bonds / gold brake status"),
    ("memory", "brake track record (holds & outcomes)"),
    ("carry", "funding-carry status"),
    ("summary", "everything at once"),
    ("pause", f"stop new trades — add {BOT_NAMES}"),
    ("resume", f"re-enable new trades — add {BOT_NAMES}"),
    ("close", f"exit a trade — add {BOT_NAMES} id|all"),
    ("reset", "clear the circuit breaker & resume"),
    ("help", "list all commands"),
]


def set_commands():
    """Tell Telegram our command list so '/' shows a menu."""
    cmds = [{"command": c, "description": d} for c, d in MENU]
    try:
        r = requests.post(f"{API}/setMyCommands", json={"commands": cmds}, timeout=15)
        print("setMyCommands:", r.json().get("ok"), flush=True)
    except Exception as e:
        # redact: a ConnectionError repr embeds /bot<TOKEN>/... — this exact line
        # re-leaked the token at boot before Wi-Fi was up (verified refutation)
        print("setMyCommands err", redact(e), flush=True)


def main():
    set_commands()
    try:
        send("🤖 TraderJoy command center is online!\n\n" + HELP)
    except Exception as e:
        print("startup send failed:", redact(e), flush=True)
    offset = None
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=40).json()
            if not r.get("ok"):
                # 409 (second poller) / 401 (rotated token) return instantly — without
                # this backoff the loop spun hot and silently dropped every command
                print("getUpdates not ok:", redact(r.get("description", r)), flush=True)
                time.sleep(max(int(r.get("parameters", {}).get("retry_after", 0)), 10))
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != CHAT:
                    continue
                text = msg.get("text") or ""
                if not text:
                    continue
                tokens = text.strip().split()
                key = tokens[0].lower().split("@")[0]
                fn = HANDLERS.get(key)
                if fn:
                    try:
                        reply = fn(tokens[1:])
                    except Exception as e:      # a handler bug must never eat a command silently
                        print(f"handler {key} error:", redact(e), flush=True)
                        reply = f"❌ {key} failed: {type(e).__name__} — check telegram_bot.log"
                    send(reply)
                elif key.startswith("/"):
                    send("Unknown command. Send /help for the list.")
        except Exception as e:
            print("loop error:", redact(e), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
