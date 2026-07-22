#!/usr/bin/env python3
"""
apex_api.py — the freqtrade-shaped REST shim the ApeX bot exposes on :8085.

ApeX is not a Freqtrade exchange, so the ApeX bot has no `api_server` of its own.
Nine ops-layer callers (dashboard, watchdog, guardian, telegram_bot, equity_logger,
weekly_digest, ...) speak exactly one dialect — freqtrade 2026.6's `/api/v1/*` — so
this shim emits that dialect and nothing else. It is a PURE READ of the engine's
in-memory state (`apex_engine.EngineState.snapshot()`); no handler ever touches the
exchange.

Rules, in priority order:

  1. NEVER return HTTP 200 with a JSON error body. portfolio_guard.py:120-124 only
     treats a bot as online when /status is a `list` AND balance.total is a non-bool
     int/float; a body like {"detail": "..."} parses fine and once false-tripped the
     circuit breaker into force-closing everything (APEX_PLAN 1.2 trap 1). Any
     internal fault leaves here as a 500 with a plain-text body.
  2. NEVER emit a bool where a number is expected. dashboard.py:332 guards with
     `isinstance(bt,(int,float)) and not isinstance(bt,bool)` (trap 2) — every numeric
     field goes through _f().
  3. The three POSTs must be cheap and idempotent. portfolio_guard.py:137-141 re-POSTs
     /stopentry every 15s for as long as the breaker is tripped (trap 3), and a
     malformed/empty body must never 500 the shim.
  4. No blocking work in a request handler. A single open dashboard tab is 6 calls per
     bot per refresh (dashboard.py:316-338) on top of the guardian's 15s poll — if any
     handler proxied to ApeX, one browser tab would become a sustained burst against
     the DEX (APEX_PLAN 4.5).

P1 scope: the engine is FLAT FOREVER, so /status is always [], /trades always empty and
/profit always zeros. The response *shapes* are the contract; the numbers arrive in P2.
"""
import secrets
import threading
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

VERSION = "apex-0.1"
MAX_TIMESCALE = 365        # clamp: /daily allocates one row per day, don't let a caller ask for 1e9
DEFAULT_TIMESCALE = 7      # freqtrade's own default when ?timescale= is omitted


def _f(x, default=0.0):
    """Coerce to a real float. Guards trap 2 — bool is a subclass of int, so a stray
    True would sail through `isinstance(x, (int, float))` at dashboard.py:332 and read
    as a $1.00 balance. float(True) is 1.0, but it is a *float*, so the bool check
    downstream can no longer misfire."""
    try:
        return float(x)
    except Exception:
        return float(default)


async def _json_body(request):
    """Best-effort dict from a POST body. The guardian re-POSTs stopentry every 15s and
    telegram_bot posts forceexit with no body at all — neither may ever 500 us (rule 3)."""
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def create_app(state):
    """Build the FastAPI app bound to one live EngineState. The state object is the
    single source of truth and the engine thread is its single writer — handlers only
    ever call snapshot(), which copies under the engine's lock."""
    cfg = state.config
    srv = cfg.get("api_server") or {}
    user = str(srv.get("username", "freqtrader"))
    password = str(srv.get("password", ""))
    stake = str(cfg.get("stake_currency", "USDC"))
    fiat = str(cfg.get("fiat_display_currency", "USD"))

    app = FastAPI(title="ApeX Omni shim", version=VERSION, docs_url=None, redoc_url=None)
    security = HTTPBasic(auto_error=True)

    def auth(creds: HTTPBasicCredentials = Depends(security)):
        """HTTPBasic freqtrader:<api_pw(8085)> — the same credentials all 9 callers send.
        compare_digest on both halves so a wrong username isn't distinguishable by timing."""
        ok_user = secrets.compare_digest(creds.username, user)
        ok_pass = secrets.compare_digest(creds.password, password)
        if not (ok_user and ok_pass):
            raise HTTPException(status_code=401, detail="Unauthorized",
                                headers={"WWW-Authenticate": "Basic"})
        return creds.username

    # ---- RULE 1: every unhandled fault becomes a plain-text 500, never a 200 body ----
    @app.exception_handler(Exception)
    def _on_unhandled(request: Request, exc: Exception):
        print(f"apex_api 500 on {request.url.path}: {type(exc).__name__}: {exc}", flush=True)
        return PlainTextResponse("Internal Server Error", status_code=500)

    # ---- GETs ----

    # freqtrade's /ping lives on the PUBLIC router (api_v1.py:82) even though every
    # caller sends Basic auth anyway — watchdog.py:96-99 must be able to reach a bot
    # whose credentials have drifted, so this one stays unauthenticated.
    @app.get("/api/v1/ping")
    def ping():
        return {"status": "pong"}

    @app.get("/api/v1/show_config")
    def show_config(_=Depends(auth)):
        snap = state.snapshot()
        return {
            "version": VERSION,
            "strategy_version": None,
            "dry_run": bool(snap["dry_run"]),
            # dashboard.py:316 reads `state`; truthiness of this whole body drives the
            # online flag at dashboard.py:321. "paused" == /stopentry has been POSTed.
            "state": "paused" if snap["paused"] else "running",
            "runmode": "dry_run" if snap["dry_run"] else "live",
            "trading_mode": str(cfg.get("trading_mode", "futures")),
            "max_open_trades": int(cfg.get("max_open_trades", 3)),
            "stake_currency": stake,
            "stake_amount": str(cfg.get("stake_amount", "unlimited")),
            "available_capital": _f(snap["balance"]),
            "stake_currency_decimals": 3,
            "exchange": "apex",
            "bot_name": str(cfg.get("bot_name", "cryptobot-apex")),
            "timeframe": str(cfg.get("timeframe", "1h")),
            "timeframe_ms": 3600000,
            "timeframe_min": 60,
            "force_entry_enable": False,
            "position_adjustment_enable": False,
            "fiat_display_currency": fiat,
        }

    @app.get("/api/v1/balance")
    def balance(_=Depends(auth)):
        snap = state.snapshot()
        total = _f(snap["balance"])
        start = _f(snap["starting_capital"])
        return {
            "currencies": [{
                "currency": stake, "free": total, "balance": total, "used": 0.0,
                "bot_owned": total, "est_stake": total, "est_stake_bot": total,
                "stake": stake, "side": "long", "is_position": False,
                "position": 0.0, "is_bot_managed": True,
            }],
            "total": total, "total_bot": total,          # consumed by equity_logger.py:38-40
            "symbol": fiat, "value": total, "value_bot": total,
            "stake": stake,
            "starting_capital": start, "starting_capital_ratio": 0.0,
            "starting_capital_pct": 0.0, "starting_capital_fiat": start,
            "starting_capital_fiat_ratio": 0.0, "starting_capital_fiat_pct": 0.0,
            "note": "Simulated balances",
        }

    @app.get("/api/v1/whitelist")
    def whitelist(_=Depends(auth)):
        pairs = [str(p) for p in ((cfg.get("exchange") or {}).get("pair_whitelist") or [])]
        return {"method": ["StaticPairList"], "length": len(pairs), "whitelist": pairs}

    # ALWAYS a bare list — portfolio_guard.py:120-124 reads a non-list as "bot offline".
    @app.get("/api/v1/status")
    def status(_=Depends(auth)):
        return state.snapshot()["positions"]

    @app.get("/api/v1/profit")
    def profit(_=Depends(auth)):
        snap = state.snapshot()
        total = _f(snap["balance"])
        start = _f(snap["starting_capital"])
        return {
            "profit_closed_coin": 0.0, "profit_closed_percent_mean": 0.0,
            "profit_closed_ratio_mean": 0.0, "profit_closed_percent_sum": 0.0,
            "profit_closed_ratio_sum": 0.0, "profit_closed_percent": 0.0,
            "profit_closed_ratio": 0.0, "profit_closed_fiat": 0.0,
            "profit_all_coin": 0.0, "profit_all_percent_mean": 0.0,
            "profit_all_ratio_mean": 0.0, "profit_all_percent_sum": 0.0,
            "profit_all_ratio_sum": 0.0, "profit_all_percent": 0.0,
            "profit_all_ratio": 0.0, "profit_all_fiat": 0.0,
            "trade_count": len(snap["closed"]) + len(snap["positions"]),
            "closed_trade_count": len(snap["closed"]),
            "first_trade_date": "", "first_trade_timestamp": 0,
            "latest_trade_date": "", "latest_trade_timestamp": 0,
            "best_pair": "", "best_rate": 0.0, "best_pair_profit_ratio": 0.0,
            "best_pair_profit_abs": 0.0,
            "winning_trades": 0, "losing_trades": 0,
            "winrate": 0.0, "expectancy": 0.0, "expectancy_ratio": 0.0,
            "profit_factor": 0.0, "max_drawdown": 0.0, "max_drawdown_abs": 0.0,
            "trading_volume": 0.0, "bot_start_timestamp": int(snap["started_at"] * 1000),
            "starting_balance": start, "current_balance": total,
        }

    @app.get("/api/v1/daily")
    def daily(timescale: int = DEFAULT_TIMESCALE, _=Depends(auth)):
        """N rows, MOST-RECENT-FIRST — dashboard.py:340 reverses this to chart it, so a
        newest-last list would silently plot the equity curve backwards."""
        snap = state.snapshot()
        days = max(1, min(int(timescale), MAX_TIMESCALE))
        today = datetime.now(timezone.utc).date()
        start = _f(snap["starting_capital"])
        rows = [{
            "date": str(today - timedelta(days=i)),
            "abs_profit": 0.0, "rel_profit": 0.0,
            "starting_balance": start, "fiat_value": 0.0, "trade_count": 0,
        } for i in range(days)]
        return {"data": rows, "fiat_display_currency": fiat, "stake_currency": stake}

    @app.get("/api/v1/trades")
    def trades(limit: int = 500, offset: int = 0, _=Depends(auth)):
        snap = state.snapshot()
        closed = snap["closed"]
        off = max(0, int(offset))
        page = closed[off:off + max(0, int(limit))]
        return {"trades": page, "trades_count": len(page),
                "offset": off, "total_trades": len(closed)}

    # ---- POSTs (rule 3: cheap, idempotent, always 200 with the freqtrade key) ----

    @app.post("/api/v1/forceexit")
    async def forceexit(request: Request, _=Depends(auth)):
        """Callers branch on .get("error") first, then .get("result") — so a
        nothing-to-do outcome is reported in `result`, never as an error (rule 1).
        portfolio_guard.py:152 sends {"tradeid": "all"} and repeats it while tripped."""
        body = await _json_body(request)
        tid = str(body.get("tradeid", "all"))
        closed = state.force_exit(tid)
        if tid == "all":
            return {"result": f"Created exit orders for all open trades ({closed})."}
        if closed:
            return {"result": f"Created exit order for trade {tid}."}
        return {"result": f"No open trade {tid} to exit."}

    @app.post("/api/v1/stopentry")
    async def stopentry(_=Depends(auth)):
        """PAUSE new entries, keep managing open ones (telegram_bot.py:320-321).
        telegram_bot.py:317 requires a dict WITHOUT an "error" key to call it a success —
        that check exists because /pause once lied when a bot was down."""
        state.set_paused(True)
        return {"status": "No more entries will occur from now. Run /reload_config to reset."}

    @app.post("/api/v1/reload_config")
    async def reload_config(_=Depends(auth)):
        """RESUME. In this system reload_config is the un-pause verb, not a config
        re-read (portfolio_guard.py:182, telegram_bot.py:329) — APEX_PLAN 4.6."""
        state.set_paused(False)
        return {"status": "Reloading config ..."}

    return app


def serve_in_thread(state):
    """Run uvicorn on a daemon thread inside the engine process, so the shim dies with
    the engine (a shim that outlived its state would report a stale flat book to the
    guardian). uvicorn skips signal-handler installation off the main thread."""
    srv = state.config.get("api_server") or {}
    conf = uvicorn.Config(create_app(state),
                          host=str(srv.get("listen_ip_address", "127.0.0.1")),
                          port=int(srv.get("listen_port", 8085)),
                          log_level=str(srv.get("verbosity", "error")),
                          access_log=False)
    server = uvicorn.Server(conf)
    thread = threading.Thread(target=server.run, name="apex-api", daemon=True)
    thread.start()
    return server, thread
