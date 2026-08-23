#!/usr/bin/env python3
"""
Trading Desk — one local dashboard for every bot.

Aggregates all three Freqtrade bots (spot / futures / braked-hold), the portfolio
guardian, and the 200-day brake board into a single auto-refreshing page served at
http://127.0.0.1:8090 . Brake states are read from the cached hourly state file so
the page never blocks on exchange calls.

Run:  ./.venv/bin/python dashboard.py     (or via launchd com.vikas.dashboard)
"""

# [cache-bust] bump on every dashboard change so a stale browser tab is obvious:
# the build shows in <title> and the no-store header forces a fresh fetch.
DASH_VERSION = "2026-08-03c"
import csv
from local_secrets import api_pw
import json
import math
import os
import sqlite3
import paper_fx                 # [PAPER EQUITY] INR->USD for the combined headline

import requests
import uvicorn

import nifty_dashboard
from requests.auth import HTTPBasicAuth
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(BASE, "guard_state.json")
BRAKE_STATE = os.path.join(BASE, "brake_alert_state.json")
EQUITY_CSV = os.path.join(BASE, "equity_history.csv")
PORTFOLIO_BOARD = os.path.join(BASE, "diversified_brake_board.json")
ACTIVITY_FEED = os.path.join(BASE, "activity_feed.jsonl")
START_EACH = 1000.0
POLY_DB = os.path.join(BASE, "polymarket.sqlite")


def _job_paused(job):
    """True if a board's scheduled generator is disabled (its plist was archived to
    disabled_plists/ in the 2026-07-20 simplification). These panels read CACHED files,
    so when the generator is off the panel is genuinely frozen — drives the 'frozen'
    badge. Self-clears the moment the job is re-enabled (plist back in LaunchAgents)."""
    return not os.path.exists(
        os.path.expanduser(f"~/Library/LaunchAgents/com.vikas.{job}.plist"))


def read_portfolio():
    """Cached diversified-braked portfolio map (mirrors the /portfolio Telegram view)."""
    if not os.path.exists(PORTFOLIO_BOARD):
        return {}
    try:
        d = json.load(open(PORTFOLIO_BOARD))
        # audit 2026-07-23: job was renamed divbrake -> diversified_brake on 07-20;
        # the old label left the panel permanently FROZEN (probed a plist that now
        # lives in disabled_plists/) even though the generator is loaded and active.
        d["_paused"] = _job_paused("diversified_brake")
        return d
    except Exception:
        return {}


def read_activity(limit=40):
    """The shared activity feed — the same push-alerts sent to Telegram, newest first."""
    if not os.path.exists(ACTIVITY_FEED):
        return []
    out = []
    try:
        lines = open(ACTIVITY_FEED).read().splitlines()[-limit:]
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    except Exception:
        return []
    return list(reversed(out))          # newest first


def read_equity():
    """Parse equity_history.csv into a list of {date, spot, futures, brakedhold,
    apex, btc_hold, basket_hold} with numeric (or null) values, for the dashboard chart."""
    if not os.path.exists(EQUITY_CSV):
        return []
    out = []
    try:
        for r in csv.DictReader(open(EQUITY_CSV)):
            row = {"date": r.get("date", "")}
            for k in ("spot", "futures", "brakedhold", "spx", "nifty", "ongc", "itc",
                      "btc", "btc_hold", "basket_hold"):
                v = r.get(k)
                try:
                    row[k] = float(v) if v not in (None, "") else None
                except ValueError:
                    row[k] = None
            out.append(row)
    except Exception:
        pass
    return out


# Map the human-facing bot name to the equity_history.csv column, so we can
# compute max drawdown from the logged daily balance series. Some bots (scalp,
# daytrade) are not tracked in the CSV because they are not on the scorecard.
BOT_EQUITY_KEY = {
    "Spot": "spot", "Futures": "futures", "Braked Hold": "brakedhold",
    "S&P 500": "spx", "Nifty 50": "nifty", "ONGC": "ongc",
    "ITC": "itc", "BTC": "btc",
}


def max_dd_for(name, balance, prof, eq_hist):
    """Best-effort max drawdown for a bot. Freqtrade bots report it in /profit;
    the apex_api shim (Nifty/ITC/ONGC) does not, so we compute it from
    equity_history.csv plus the current balance. If both sources exist, use the
    larger (more conservative) number."""
    # 1) what the bot's own /profit endpoint already reports
    prof_dd = (prof.get("max_drawdown") or 0.0) * 100.0

    # 2) historical series from equity_history.csv, with current balance appended
    key = BOT_EQUITY_KEY.get(name)
    hist = []
    if key and eq_hist:
        for r in eq_hist:
            v = r.get(key)
            if v is not None and v > 0:
                hist.append(v)
    if balance and balance > 0:
        hist.append(float(balance))

    csv_dd = 0.0
    if hist:
        peak = hist[0]
        m = 0.0
        for v in hist:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > m:
                m = dd
        csv_dd = m * 100.0

    return round(max(prof_dd, csv_dd), 2)


BOTS = [
    # Spot Trend-Follow parked 2026-07-28: edge expired 2024 (negative for 19 months,
    # backtest confirms -0.43% over the live window too — not a backtest/live gap).
    # The +41% was a 2022-23 artifact. 1h EMA50 exit sells on noise (avg hold 13h).
    # Plist archived to disabled_plists/. Port 8080 free. Config/strategy kept for rebuild.
    ("Futures", 8081, api_pw(8081), "Short-only · 4h · funding harvest (backtest +24% PF1.56 DD8% net of funding, 6yr)"),
    ("ETH Futures", 8092, api_pw(8092), "Short-only ETH · 4h · ETH-tuned ADX30/CONF4 (backtest +11% PF1.92 DD3% net of funding, 6yr)"),
    ("Braked Hold", 8082, api_pw(8082), "200-day brake · spot · WINNER (backtest +1219% PF11)"),
    ("Scalp", 8083, api_pw(8083), "RSI-2 dip reversion · 1h · SOL+LINK+ADA portfolio on OKX perp (backtest +23.7% PF1.60 Sharpe1.71 DD3.9% net of fees, 4.6yr, walk-forward validated, p=0.0011)"),
    # Day Trade ORB killed 2026-07-28: zero-edge signal (event study n=30k, 108-config grid,
    # short side all failed OOS). Crypto has no overnight info gap, so 00:00 UTC "opening
    # range" is arbitrary noise. Plist archived to disabled_plists/. Port 8084 free.
    # ApeX removed 2026-07-23: synthetic-only paper engine whose balance inflated the
    # real-money headline to +2641%/$301k (synthetic price-path bug, not real capital).
    # Kept out of BOTS so the dashboard stops polling :8085 and the combined total is honest.
    ("S&P 500", 8086, api_pw(8086), "SMA cross · SPY paper"),
    ("Nifty 50", 8087, api_pw(8087), "SMA cross · NIFTYBEES paper"),
    ("ONGC", 8088, api_pw(8088), "Dividend · ONGC 7.4%"),
    ("ITC", 8089, api_pw(8089), "Dividend · ITC 5.7%"),
    # 8091, NOT 8090 — this dashboard itself serves on 8090; a bot there would collide.
    ("BTC", 8091, api_pw(8091), "SMA cross · BTC-USD paper"),
]

app = FastAPI(title="Trading Desk")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
app.mount("/nifty", nifty_dashboard.app, name="nifty_dashboard")

CRYPTO_COINS = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC",
                "DOGE", "LINK", "BNB", "AVAX", "DOT", "TRX"]

# The exact macro tickers the chart selector offers (CHART_ASSETS in the page JS).
# The macro branch feeds `asset` straight into yfinance, so gate it to this allowlist
# rather than fetching whatever arbitrary string a caller supplies (unvalidated input).
MACRO_ASSETS = {"SPY", "QQQ", "^NSEI", "TLT", "GLD", "PPLT"}


@app.get("/api/candles")
def candles(asset: str):
    """OHLCV daily candles + 200-day line for one asset. Crypto comes from the
    braked-hold bot (sma200 pre-computed); macro comes from yfinance."""
    a = asset.upper()
    if a not in CRYPTO_COINS and a not in MACRO_ASSETS:
        return JSONResponse({"error": "unknown asset"}, status_code=400)
    try:
        if a in CRYPTO_COINS:
            r = requests.get("http://127.0.0.1:8082/api/v1/pair_candles",
                             params={"pair": f"{a}/USDT", "timeframe": "1d", "limit": 600},
                             auth=HTTPBasicAuth("freqtrader", api_pw(8082)), timeout=10).json()
            cols, data = r["columns"], r["data"]
            di, oi, hi, li, ci = (cols.index(k) for k in ("date", "open", "high", "low", "close"))
            vi = cols.index("volume") if "volume" in cols else None
            si = cols.index("sma200") if "sma200" in cols else None
            cndl, sma = [], []
            for row in data:
                t = row[di][:10]
                cndl.append({"time": t, "open": row[oi], "high": row[hi],
                             "low": row[li], "close": row[ci],
                             "volume": row[vi] if vi is not None else 0})
                if si is not None and row[si] is not None:
                    sma.append({"time": t, "value": row[si]})
            return JSONResponse({"candles": cndl, "sma": sma, "label": f"{a}/USDT"})
        # macro via yfinance
        import pandas as pd
        import yfinance as yf
        df = yf.download(a, period="2y", interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return JSONResponse({"error": "no data for " + a})
        def col(name):
            c = df[name]
            return c.iloc[:, 0] if hasattr(c, "columns") else c
        o, h, l, c = col("Open"), col("High"), col("Low"), col("Close")
        vv = col("Volume") if "Volume" in df.columns else None
        sma200 = c.rolling(200).mean()
        cndl, sma = [], []
        for i in range(len(df)):
            t = str(df.index[i].date())
            vol = float(vv.iloc[i]) if vv is not None and pd.notna(vv.iloc[i]) else 0
            cndl.append({"time": t, "open": float(o.iloc[i]), "high": float(h.iloc[i]),
                         "low": float(l.iloc[i]), "close": float(c.iloc[i]), "volume": vol})
            if pd.notna(sma200.iloc[i]):
                sma.append({"time": t, "value": float(sma200.iloc[i])})
        return JSONResponse({"candles": cndl, "sma": sma, "label": a})
    except Exception as e:
        return JSONResponse({"error": str(e)[:120]})


# ---- live market data (order book + recent trades), read-only ----------------
# Sourced via the SAME reachable-exchange picker the bots use (brake_alerts), so it
# rides the US-IP binance-451 fallback to okx/kraken automatically. Cached across
# requests; on any error we drop the handle so the next call re-picks a live one.
_EX = {"name": None, "ex": None}


def _exchange():
    if _EX["ex"] is None:
        import brake_alerts as ba
        _EX["name"], _EX["ex"] = ba.pick_exchange()
    return _EX["name"], _EX["ex"]


def _market(asset, fn):
    a = asset.upper()
    if a not in CRYPTO_COINS:
        return None, None, JSONResponse({"error": "crypto pairs only"})
    try:
        name, ex = _exchange()
        if ex is None:
            return None, None, JSONResponse({"error": "no reachable exchange right now"})
        return name, ex, None
    except Exception as e:
        _EX["ex"] = None
        return None, None, JSONResponse({"error": f"{type(e).__name__}: {str(e)[:80]}"})


@app.get("/api/orderbook")
def orderbook(asset: str):
    name, ex, err = _market(asset, "ob")
    if err:
        return err
    a = asset.upper()
    try:
        ob = ex.fetch_order_book(f"{a}/USDT", limit=25)
        # some exchanges return [price, amount, ...extra]; take the first two fields only
        bids = [[float(x[0]), float(x[1])] for x in ob.get("bids", [])[:12]]
        asks = [[float(x[0]), float(x[1])] for x in ob.get("asks", [])[:12]]
        return JSONResponse({"bids": bids, "asks": asks, "source": name, "pair": f"{a}/USDT"})
    except Exception as e:
        _EX["ex"] = None
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:80]}"})


@app.get("/api/trades")
def recent_trades(asset: str):
    name, ex, err = _market(asset, "tr")
    if err:
        return err
    a = asset.upper()
    try:
        tr = ex.fetch_trades(f"{a}/USDT", limit=30)
        out = [{"price": float(t["price"]), "amount": float(t["amount"]),
                "side": t.get("side") or "", "time": (t.get("datetime") or "")[11:19]}
               for t in tr[-30:]]
        out.reverse()                              # newest first
        return JSONResponse({"trades": out, "source": name, "pair": f"{a}/USDT"})
    except Exception as e:
        _EX["ex"] = None
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:80]}"})


@app.get("/api/depth")
def depth(asset: str):
    """Deeper book with CUMULATIVE size, for the depth-curve chart. bids accumulate
    outward from the best bid (descending price); asks from the best ask (ascending)."""
    name, ex, err = _market(asset, "depth")
    if err:
        return err
    a = asset.upper()
    try:
        ob = ex.fetch_order_book(f"{a}/USDT", limit=100)
        bids, c = [], 0.0
        for x in ob.get("bids", [])[:50]:          # ccxt returns bids best-first (descending)
            c += float(x[1]); bids.append([float(x[0]), c])
        asks, c = [], 0.0
        for x in ob.get("asks", [])[:50]:          # asks best-first (ascending)
            c += float(x[1]); asks.append([float(x[0]), c])
        return JSONResponse({"bids": bids, "asks": asks, "source": name, "pair": f"{a}/USDT"})
    except Exception as e:
        _EX["ex"] = None
        return JSONResponse({"error": f"{type(e).__name__}: {str(e)[:80]}"})


def api(port, pw, ep):
    try:
        return requests.get(f"http://127.0.0.1:{port}/api/v1/{ep}",
                            auth=HTTPBasicAuth("freqtrader", pw), timeout=5).json()
    except Exception:
        return None


# 2026-07-29: map bot display name -> config file, to read dry_run_wallet.
# show_config API doesn't expose dry_run_wallet, so we read the file directly.
_BOT_CONFIG_MAP = {
    "Futures": "config_futures.json",
    "ETH Futures": "config_eth_futures.json",
    "Braked Hold": "config_braked_v2.json",
    "Scalp": "config_sol_scalp.json",
    "S&P 500": "config_spx.json",
    "Nifty 50": "config_nifty.json",
    "ONGC": "config_ongc.json",
    "ITC": "config_itc.json",
    "BTC": "config_btc.json",
}
_bot_config_cache = {}   # name -> (dry_run_wallet, mtime) so we only re-read on change

def _dry_run_wallet_for(bot_name):
    """Read dry_run_wallet from the bot's config file (cached by mtime).
    Returns None if the config can't be read — caller falls back to START_EACH."""
    path = os.path.join(BASE, _BOT_CONFIG_MAP.get(bot_name, ""))
    if not path or not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        cached = _bot_config_cache.get(bot_name)
        if cached and cached[1] == mtime:
            return cached[0]
        import json as _json
        cfg = _json.load(open(path))
        wallet = cfg.get("dry_run_wallet")
        if isinstance(wallet, (int, float)) and not isinstance(wallet, bool):
            _bot_config_cache[bot_name] = (wallet, mtime)
            return wallet
    except Exception:
        pass
    return None


@app.get("/api/overview")
def overview():
    bots, tot_bal = [], 0.0
    eq_hist = read_equity()
    for name, port, pw, desc in BOTS:
        cfg = api(port, pw, "show_config") or {}
        bal = api(port, pw, "balance") or {}
        prof = api(port, pw, "profit") or {}
        st = api(port, pw, "status")
        wl = api(port, pw, "whitelist") or {}
        online = bool(cfg or bal or st is not None)
        opens = []
        if st:
            for t in st:
                if not isinstance(t, dict):
                    continue
                opens.append({
                    "pair": t.get("pair", "?"),
                    "dir": "SHORT" if t.get("is_short") else "LONG",
                    "profit": (t.get("profit_ratio") or 0) * 100,
                    "stake": t.get("stake_amount") or 0,
                })
        bt = bal.get("total")
        has_bal = isinstance(bt, (int, float)) and not isinstance(bt, bool)
        b = bt if has_bal else 0
        # each bot reports in its own currency. Freqtrade-native signal is
        # stake_currency; the apex_api shim also echoes pair as "X/INR". Convert
        # INR -> USD so the combined headline sums in ONE currency.
        sc = (cfg.get("stake_currency") or "").upper()
        pair = (cfg.get("pair") or "").upper()
        currency = "INR" if (sc == "INR" or pair.endswith("/INR")) else "USD"
        if has_bal:
            tot_bal += paper_fx.to_usd(b, currency)
        # 2026-07-29: each bot's ACTUAL starting balance (dry_run_wallet from
        # the config FILE, since show_config doesn't expose it), converted to
        # USD — not a flat $1000. The old START_EACH*count assumed all 9 bots
        # started with $1000 USD, but INR bots started with ~₹9656 (~$100) and
        # Scalp/BTC started with $250/$500. The headline showed -44% when the
        # real P&L is ~0%.
        start_wallet = _dry_run_wallet_for(name)
        if not isinstance(start_wallet, (int, float)) or isinstance(start_wallet, bool):
            start_wallet = START_EACH   # fallback if config file missing
        start_usd = paper_fx.to_usd(start_wallet, currency) if has_bal else 0
        # 30-day realized-equity sparkline from the /daily endpoint (most-recent-first)
        eq = []
        daily = api(port, pw, "daily?timescale=30")
        if daily and daily.get("data"):
            rows = list(reversed(daily["data"]))  # -> chronological
            if rows:
                eq.append(rows[0].get("starting_balance", START_EACH) or START_EACH)
                for drow in rows:
                    eq.append((drow.get("starting_balance", 0) or 0)
                              + (drow.get("abs_profit", 0) or 0))
        bots.append({
            "name": name, "desc": desc, "online": online, "has_balance": has_bal,
            "state": cfg.get("state", "?"),
            "balance": b,
            "profit_pct": prof.get("profit_closed_percent", 0) or 0,
            "trades": prof.get("closed_trade_count", 0) or 0,
            "winrate": (prof.get("winrate", 0) or 0) * 100,
            "max_dd": max_dd_for(name, b, prof, eq_hist),
            "open": opens, "equity": eq,
            "currency": currency,    # [PAPER EQUITY] lets the card show ₹ vs $ per-bot
            "start_usd": start_usd,  # 2026-07-29: actual starting balance in USD
            # the coin universe the bot is monitoring (base symbols), incl. any open-trade
            # pairs Freqtrade auto-adds; lets the card show "watching" even when flat
            "watching": [p.split("/")[0] for p in (wl.get("whitelist") or [])],
        })

    g = {}
    if os.path.exists(GUARD):
        try:
            g = json.load(open(GUARD))
        except Exception:
            g = {}

    brake = []
    if os.path.exists(BRAKE_STATE):
        try:
            for coin, d in json.load(open(BRAKE_STATE)).items():
                price, sma = d.get("price", 0), d.get("sma", 0)
                brake.append({
                    "coin": coin, "state": d.get("state"),
                    "price": price,
                    "gap": ((price / sma - 1) * 100) if sma else 0,
                })
        except Exception:
            pass
    brake.sort(key=lambda c: (c["state"] != "above", -c["gap"]))
    hold = sum(1 for c in brake if c["state"] == "above")

    try:
        import brake_memory
        mem = brake_memory.stats({c["coin"]: c["price"] for c in brake})
    except Exception:
        mem = None

    # headline math counts ONLY bots whose balance call succeeded — an offline bot
    # used to read as a fake -33% "loss" in the header (audit finding)
    # 2026-07-29: start_total now sums each bot's ACTUAL dry_run_wallet (converted
    # to USD), not a flat $1000 per bot. The old math assumed all 9 bots started
    # with $1000 USD, but INR bots started with ~₹9656 (~$100) and Scalp/BTC
    # started with $250/$500 — producing a fake -44% headline.
    n_reporting = sum(1 for x in bots if x.get("has_balance"))
    start_total = sum(x.get("start_usd", 0) for x in bots if x.get("has_balance"))
    missing = [x["name"] for x in bots if not x.get("has_balance")]

    # equal-weighted average P&L% — each bot counts 1/n regardless of capital
    eq_pcts = []
    for x in bots:
        if x.get("has_balance"):
            bal_usd = paper_fx.to_usd(x["balance"], x.get("currency", "USD"))
            start = x.get("start_usd", 0)
            if start:
                eq_pcts.append((bal_usd / start - 1) * 100)
    equal_weighted_pct = sum(eq_pcts) / len(eq_pcts) if eq_pcts else 0

    usdinr = paper_fx.get_usdinr()
    return JSONResponse({
        "bots": bots,
        "total_balance": tot_bal,
        "total_currency": "USD",   # [PAPER EQUITY] INR bots converted -> honest combined $
        "total_pnl_pct": (tot_bal / start_total - 1) * 100 if start_total else 0,
        "equal_weighted_pct": equal_weighted_pct,
        "usdinr_rate": usdinr,
        "bots_reporting": n_reporting, "bots_missing": missing,
        "guardian": {
            "tripped": bool(g.get("breaker_tripped", False)),
            "paused": bool(g.get("paused_by_guard", False)),
            "peak": g.get("peak_balance", 0) or 0,
        },
        "brake": brake, "brake_hold": hold, "brake_total": len(brake),
        "portfolio": read_portfolio(),
        "activity": read_activity(),
        "memory": mem,
        "equity_history": eq_hist,
    })

# ---- backtest results panel -------------------------------------------------
# Reads the static result JSONs from nifty_backtest/. These are offline research
# artifacts (not live state), so they're served from a separate endpoint and
# loaded once on page load — not polled every 15s like /api/overview.
BACKTEST_DIR = os.path.join(BASE, "nifty_backtest")

# (filename, display title, sort order). Each file is a self-contained result
# set; the panel groups them under their title. Sort order controls panel order.
BACKTEST_FILES = [
    ("btc_faber_results.json",            "BTC 200DMA · 3-year after-tax",        0),
    ("backtest_india_tax_results.json",   "BrakedHoldV2 · 1-year India after-tax", 1),
    ("walkforward_results.json",          "Nifty 200DMA · walk-forward validated",  2),
    ("results.json",                      "Nifty SMA cross · full history",         3),
    ("equity_tax_model_results.json",     "Indian equity · after-tax by regime",    4),
]


def _safe_num(v):
    """Coerce a backtest metric to float; non-finite (inf/NaN) -> None (can't JSON-serialize
    and the UI shows '∞' instead). Strings/None pass through as-is."""
    if v is None:
        return None
    if isinstance(v, str):
        return None if v == "inf" else v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if math.isfinite(v) else None
    return None


def _normalize_result(r):
    """Pull the common metrics out of a result dict, tolerating the slightly
    different schemas the various backtest scripts produce."""
    return {
        "variant":   r.get("variant", r.get("strategy", "?")),
        "total_ret": _safe_num(r.get("total_return_pct", r.get("aftertax_total_pct"))),
        "cagr":      _safe_num(r.get("cagr_pct", r.get("aftertax_cagr_pct"))),
        "max_dd":    _safe_num(r.get("max_drawdown_pct")),
        "trades":    r.get("trades", 0),
        "win_rate":  _safe_num(r.get("win_rate_pct")),
        "pf":        _safe_num(r.get("profit_factor")),
        "sharpe":    _safe_num(r.get("sharpe")),
        "tax_drag":  _safe_num(r.get("tax_drag_pct")),
        "avg_hold":  _safe_num(r.get("avg_hold_bars", r.get("avg_hold_days"))),
    }


@app.get("/api/backtests")
def backtests():
    """Static backtest result sets from nifty_backtest/*.json. Loaded once on
    page load — not polled. Returns a list of {title, order, meta, results}."""
    out = []
    for fname, title, order in BACKTEST_FILES:
        path = os.path.join(BACKTEST_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            raw = json.load(open(path))
        except Exception:
            continue
        # multiple schemas: a flat list of variants under "results", a structured
        # dict with "strategy" (dict) + "dca_benchmark" (backtest_india_tax),
        # or a walkforward dict with "in_sample_reference" + "clean_holdout".
        # Check most-specific first — walkforward also has a "strategy" key but
        # it's a string description, not a result dict.
        meta = {}
        results = []
        if isinstance(raw, dict) and "in_sample_reference" in raw:
            # walkforward: in_sample_reference + clean_holdout{in_sample, oos}
            meta = {"strategy": raw.get("strategy", "")}
            ho = raw.get("clean_holdout", {})
            results = [
                {**_normalize_result(ho.get("in_sample", {})),
                 "variant": "In-sample (2009-2022)"},
                {**_normalize_result(ho.get("oos", {})),
                 "variant": "Out-of-sample (2023-2026)"},
            ]
        elif isinstance(raw, dict) and "results" in raw:
            meta = {k: v for k, v in raw.items() if k != "results"
                    and not isinstance(v, (list, dict))}
            results = [_normalize_result(r) for r in raw["results"]
                       if isinstance(r, dict)]
        elif isinstance(raw, dict) and "strategy" in raw \
                and isinstance(raw.get("strategy"), dict):
            # backtest_india_tax: strategy (dict) + dca_benchmark + metadata
            meta = {k: v for k, v in raw.items()
                    if k not in ("strategy", "dca_benchmark")
                    and not isinstance(v, (list, dict))}
            results = [_normalize_result(raw["strategy"]),
                       _normalize_result(raw["dca_benchmark"])]
        elif isinstance(raw, list):
            results = [_normalize_result(r) for r in raw if isinstance(r, dict)]
        if results:
            out.append({"title": title, "order": order, "meta": meta,
                        "results": results})
    out.sort(key=lambda x: x["order"])
    return JSONResponse(out)


# ---- backtest analyst reports ----------------------------------------------
# Reads the timestamped analysis JSONs from backtest_analyses/. These are LLM-
# generated reports (Claude analyzing the backtest results), served from a
# separate endpoint and loaded once on page load alongside /api/backtests.
ANALYSES_DIR = os.path.join(BASE, "backtest_analyses")


@app.get("/api/backtest_analyses")
def backtest_analyses():
    """LLM-generated backtest analysis reports from backtest_analyses/*.json.
    Returns a list sorted newest-first, each with timestamp, summary, and the
    full structured analysis. Loaded once on page load — not polled."""
    out = []
    if not os.path.isdir(ANALYSES_DIR):
        return JSONResponse(out)
    try:
        files = sorted(os.listdir(ANALYSES_DIR), reverse=True)
    except Exception:
        return JSONResponse(out)
    for fname in files:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(ANALYSES_DIR, fname)) as f:
                data = json.load(f)
        except Exception:
            continue
        analysis = data.get("analysis", {})
        risk = analysis.get("overfitting_risk", {})
        out.append({
            "file": fname,
            "timestamp": data.get("timestamp", ""),
            "checksum": data.get("checksum", ""),
            "files_analyzed": data.get("files_analyzed", []),
            "summary": analysis.get("summary", ""),
            "overfitting_risk": risk.get("level", "?") if isinstance(risk, dict) else str(risk),
            "drawdown_analysis": analysis.get("drawdown_analysis", ""),
            "robustness": analysis.get("robustness", ""),
            "benchmark_comparison": analysis.get("benchmark_comparison", ""),
            "improvement_candidates": analysis.get("improvement_candidates", []),
            "red_flags": analysis.get("red_flags", []),
            "confidence": analysis.get("confidence", "?"),
        })
    return JSONResponse(out)


@app.get("/api/brake/live")
def brake_live():
    """On-demand FRESH brake status straight from the exchange (bypasses the hourly
    cache). Slower (~2-3s) — only runs when the button is clicked."""
    import brake_alerts as ba
    watch = ba.load_json(ba.WATCHLIST, {"coins": ba.DEFAULT_WATCH}).get("coins", ba.DEFAULT_WATCH)
    src, ex = ba.pick_exchange()
    if ex is None:
        return JSONResponse({"error": "no exchange reachable right now"})
    coins = []
    for c in watch:
        try:
            st, price, sma = ba.brake_state(ex, c)
            coins.append({"coin": c, "state": st, "price": price,
                          "gap": (price / sma - 1) * 100})
        except Exception as e:
            coins.append({"coin": c, "error": str(e)[:40]})
    coins.sort(key=lambda c: (c.get("state") != "above", -(c.get("gap") if c.get("gap") is not None else -999)))
    return JSONResponse({"source": src, "coins": coins,
                         "hold": sum(1 for c in coins if c.get("state") == "above"),
                         "total": len(coins)})


@app.get("/api/memory/full")
def memory_full():
    """The complete track-record text (same as the /memory Telegram command)."""
    import brake_memory
    prices = {}
    if os.path.exists(BRAKE_STATE):
        try:
            prices = {c: d.get("price") for c, d in json.load(open(BRAKE_STATE)).items()}
        except Exception:
            pass
    return JSONResponse({"text": brake_memory.summary(prices)})


# ---- Polymarket copy-trading bot endpoints ----
def _poly_conn():
    """Open a short-lived SQLite connection to the polymarket DB."""
    conn = sqlite3.connect(POLY_DB)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/api/polymarket/india")
def poly_india():
    """India Desk: India-relevant Polymarket markets with live gamma data +
    last whale activity. Data-only view (MeitY blocked the platform in India
    Apr 2026 — the bot never places orders, it just reads public data)."""
    try:
        conn = _poly_conn()
        rows = conn.execute(
            "SELECT * FROM india_markets ORDER BY volume_24h DESC"
        ).fetchall()
        # Filter recent trades with the boundary-aware matcher (SQL LIKE '%ipl%'
        # would match "diplomatic").
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "polymarket"))
        from india_watch import is_india_relevant
        raw_trades = conn.execute(
            "SELECT market, side, price, size, timestamp FROM raw_trades "
            "WHERE timestamp >= datetime('now','-7 days') ORDER BY timestamp DESC LIMIT 5000"
        ).fetchall()
        trades = [dict(t) for t in raw_trades
                  if is_india_relevant(t["market"])][:12]
        conn.close()
        return JSONResponse({
            "markets": [dict(r) for r in rows],
            "recent_india_trades": [dict(t) for t in trades],
            "note": "India's MeitY blocked Polymarket (Apr 2026); VPN providers "
                    "warned. Data-only view — no orders placed.",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/polymarket/overview")
def poly_overview():
    """Summary: equity, PnL, drawdown, wallet count, open positions, trade stats."""
    try:
        conn = _poly_conn()
        wallets = conn.execute("SELECT COUNT(*) c FROM wallet_universe WHERE is_active=1").fetchone()["c"]
        snap = conn.execute("SELECT * FROM hourly_pnl_snapshot ORDER BY snapshot_at DESC LIMIT 1").fetchone()
        scored = conn.execute("SELECT COUNT(DISTINCT wallet_address) c FROM wallet_scores").fetchone()["c"]
        open_pos = conn.execute("SELECT COUNT(*) c FROM paper_positions WHERE status='OPEN'").fetchone()["c"]
        total_trades = conn.execute("SELECT COUNT(*) c FROM raw_trades").fetchone()["c"]
        decisions = conn.execute("SELECT decision, COUNT(*) c FROM trade_decisions GROUP BY decision").fetchall()
        missed = conn.execute("SELECT COUNT(*) c FROM decision_journal WHERE is_missed_winner=1").fetchone()["c"]
        rules = conn.execute("SELECT COALESCE(MAX(id),0) c FROM scoring_versions").fetchone()["c"]
        conn.close()
        return JSONResponse({
            "equity": snap["equity"] if snap else 1000.0,
            "pnl": snap["net_pnl"] if snap else 0.0,
            "drawdown_pct": snap["drawdown_pct"] if snap else 0.0,
            "peak_equity": snap["peak_equity"] if snap else 1000.0,
            "open_positions": open_pos,
            "wallets": wallets,
            "scored_wallets": scored,
            "total_trades": total_trades,
            "decisions": {r["decision"]: r["c"] for r in decisions},
            "missed_winners": missed,
            "rule_version": rules,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/polymarket/wallets")
def poly_wallets():
    """Top 20 ranked wallets with scores and category edge."""
    try:
        conn = _poly_conn()
        rows = conn.execute("""
            SELECT ws.wallet_address, ws.composite_score, ws.roi_score,
                   ws.consistency_score, ws.copyability_score, ws.entry_timing_score,
                   ws.category_edge, ws.rank, ws.scored_at,
                   wu.raw_leaderboard_data
            FROM wallet_scores ws
            JOIN wallet_universe wu ON wu.wallet_address = ws.wallet_address
            WHERE ws.scored_at = (SELECT MAX(scored_at) FROM wallet_scores)
            ORDER BY ws.rank ASC LIMIT 20
        """).fetchall()
        conn.close()
        wallets = []
        for r in rows:
            data = json.loads(r["raw_leaderboard_data"] or "{}")
            edge = json.loads(r["category_edge"] or "{}")
            wallets.append({
                "rank": r["rank"],
                "address": r["wallet_address"],
                "short": r["wallet_address"][:6] + "\u2026" + r["wallet_address"][-4:],
                "pseudonym": data.get("pseudonym") or data.get("name") or "",
                "composite": r["composite_score"],
                "roi": r["roi_score"],
                "consistency": r["consistency_score"],
                "copyability": r["copyability_score"],
                "timing": r["entry_timing_score"],
                "category_edge": edge,
                "volume": data.get("volume_24h") or data.get("volume") or 0,
                "trades": data.get("trade_count") or 0,
            })
        return JSONResponse(wallets)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/polymarket/positions")
def poly_positions():
    """Open paper positions + recent closed ones."""
    try:
        conn = _poly_conn()
        open_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status='OPEN' ORDER BY opened_at DESC"
        ).fetchall()
        closed_rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT 10"
        ).fetchall()
        conn.close()
        def pos_dict(r):
            return {
                "id": r["id"], "wallet": r["wallet_address"][:8],
                "market": r["market"][:40], "side": r["side"],
                "entry": r["entry_price"], "size": r["position_size"],
                "opened": r["opened_at"], "closed": r["closed_at"],
                "pnl": r["realized_pnl"] if r["realized_pnl"] is not None else 0.0,
                "exit": r["exit_reason"] or "",
            }
        return JSONResponse({
            "open": [pos_dict(r) for r in open_rows],
            "closed": [pos_dict(r) for r in closed_rows],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/polymarket/rules")
def poly_rules():
    """Rule version history + recent decision mix."""
    try:
        conn = _poly_conn()
        versions = conn.execute(
            "SELECT id, effective_from, rule_set, change_reason FROM scoring_versions ORDER BY id DESC LIMIT 10"
        ).fetchall()
        mix = conn.execute(
            "SELECT decision, COUNT(*) c FROM trade_decisions GROUP BY decision"
        ).fetchall()
        tiers = conn.execute(
            "SELECT bet_size_tier, COUNT(*) c FROM trade_decisions WHERE decision='COPY' GROUP BY bet_size_tier"
        ).fetchall()
        conn.close()
        return JSONResponse({
            "versions": [
                {"id": v["id"], "from": v["effective_from"],
                 "rules": json.loads(v["rule_set"] or "{}"), "reason": v["change_reason"] or ""}
                for v in versions
            ],
            "decision_mix": {r["decision"]: r["c"] for r in mix},
            "bet_tiers": {r["bet_size_tier"]: r["c"] for r in tiers},
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/polymarket/timeline")
def poly_timeline():
    """Hourly PnL snapshots for the equity chart."""
    try:
        conn = _poly_conn()
        rows = conn.execute(
            "SELECT snapshot_at, equity, net_pnl, drawdown_pct, open_positions_count "
            "FROM hourly_pnl_snapshot ORDER BY snapshot_at ASC LIMIT 200"
        ).fetchall()
        conn.close()
        return JSONResponse([
            {"t": r["snapshot_at"], "eq": r["equity"], "pnl": r["net_pnl"],
             "dd": r["drawdown_pct"], "pos": r["open_positions_count"]}
            for r in rows
        ])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/")
def index_root():
    # Self-healing against frozen bfcache tabs: redirect to a versioned path so a
    # stale tab (running old JS that ignores no-store) is forced onto a fresh URL after
    # the first hard refresh. Every future deploy changes the path -> auto-redirect.
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/d/{DASH_VERSION}/", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/d/{build}/", response_class=HTMLResponse)
def index(build: str):
    # [cache-bust] no-store forces the browser to re-fetch every load, so a
    # stale tab never silently shows an old build after a dashboard deploy.
    # __DASH_VER__ is substituted from DASH_VERSION at request time.
    # __DASH_BUILD__ is the same value, exposed to JS for the stale-tab hard-reload guard.
    return HTMLResponse(
        PAGE.replace("__DASH_VER__", DASH_VERSION).replace("__DASH_BUILD__", DASH_VERSION),
        headers={"Cache-Control": "no-store, max-age=0"},
    )


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>Trading Desk · build __DASH_VER__</title>
<style>
  :root{
    --ground:#0E1420; --surface:#161E2E; --surface-2:#1C2739; --line:#26314A;
    --text:#E6EAF2; --muted:#8A97B2; --faint:#5C6884;
    --amber:#F0A83C; --teal:#3FC7A8; --brick:#E5674E;
    --mono:ui-monospace,"SF Mono","Menlo","Consolas",monospace;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  :root[data-theme="light"]{
    --ground:#F1F4F9; --surface:#FFFFFF; --surface-2:#EDF1F7; --line:#DBE1EC;
    --text:#16202E; --muted:#586880; --faint:#93A0B4;
    --amber:#C77E12; --teal:#1A9C80; --brick:#CD4B32;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--ground);color:var(--text);font-family:var(--sans);
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:min(1820px,96vw);margin:0 auto;padding:0 26px 60px;}
  .deskgrid{display:grid;grid-template-columns:1fr;gap:26px;margin-top:26px;}
  @media(min-width:1200px){.deskgrid{grid-template-columns:1.65fr 1fr;align-items:start;}}
  .deskmain{min-width:0;} .deskrail{min-width:0;}
  .deskrail .board{grid-template-columns:repeat(3,minmax(0,1fr));}
  .deskrail .brakehead:first-child .sec, .deskmain > .sec:first-child{margin-top:0!important;}
  .panelrow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin-top:26px;}
  .panelrow > div{min-width:0;}
  .panelrow > div > .sec{margin-top:0;}
  @media(max-width:1000px){.panelrow{grid-template-columns:1fr;}}
  .mono{font-family:var(--mono);font-variant-numeric:tabular-nums;}
  .label{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);}
  .pos{color:var(--teal);} .neg{color:var(--brick);}

  header{display:flex;flex-wrap:wrap;gap:18px 30px;align-items:center;justify-content:space-between;
    padding:22px 0;border-bottom:1px solid var(--line);margin-bottom:24px;}
  .brand{display:flex;align-items:center;gap:12px;}
  .brand h1{font-size:20px;letter-spacing:-.01em;margin:0;font-weight:700;}
  .brand small{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--muted);}
  .glyph{width:30px;height:30px;}
  .totals{display:flex;gap:34px;align-items:flex-end;}
  .totals .t .label{display:block;margin-bottom:5px;}
  .totals .big{font-family:var(--mono);font-size:26px;font-weight:700;letter-spacing:-.01em;}
  .live{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;color:var(--faint);}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--teal);}
  .dot.pulse{animation:p 2s infinite;}
  @keyframes p{0%,100%{opacity:1;}50%{opacity:.3;}}

  .toolbar{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:22px;}
  .tbtn{font-family:var(--mono);font-size:12.5px;font-weight:600;padding:9px 15px;border-radius:9px;cursor:pointer;
    background:var(--surface);color:var(--text);border:1px solid var(--line);transition:all .15s ease;
    display:inline-flex;align-items:center;gap:7px;}
  .tbtn:hover{border-color:var(--amber);color:var(--amber);}
  .tbtn:disabled{opacity:.55;cursor:wait;}
  .tbtn:focus-visible{outline:2px solid var(--amber);outline-offset:2px;}
  .modal{position:fixed;inset:0;background:rgba(6,10,18,.74);display:none;align-items:flex-start;
    justify-content:center;padding:56px 18px;z-index:50;overflow:auto;}
  .modal.open{display:flex;}
  .modal-content{background:var(--surface);border:1px solid var(--line);border-radius:14px;max-width:620px;
    width:100%;padding:22px 24px;box-shadow:0 24px 70px rgba(0,0,0,.55);}
  .modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;}
  .modal-head h3{margin:0;font-size:16px;letter-spacing:-.01em;}
  .modal-close{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;
    width:30px;height:30px;cursor:pointer;font-size:15px;line-height:1;}
  .modal-close:hover{color:var(--text);border-color:var(--faint);}
  .modal-content pre{font-family:var(--mono);font-size:13px;white-space:pre-wrap;line-height:1.75;margin:0;}
  .mboard{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}
  .msub{font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:13px;}
  .guard{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;padding:12px 16px;margin-bottom:22px;
    background:var(--surface);border:1px solid var(--line);border-radius:11px;font-family:var(--mono);font-size:13px;}
  .guard.tripped{border-color:var(--brick);background:rgba(229,103,78,.08);}
  .chip{display:inline-flex;align-items:center;gap:7px;}
  .chip .s{width:9px;height:9px;border-radius:50%;}

  h2.sec{font-size:12px;font-family:var(--mono);letter-spacing:.16em;text-transform:uppercase;
    color:var(--muted);margin:0 0 13px;font-weight:600;}

  /* ---- BOT TABLE ----
     This is a MONITOR surface: 11 bots have to fit one screen. The old 3-col tile
     grid gave every bot a full card (wallet + P&L + trades + win-bar + 30-day
     sparkline + open positions + watchlist) and overflowed the viewport at 9 bots
     (2026-07-22). Now: one scannable row per bot, detail on demand via expand. */
  .bots{display:flex;flex-direction:column;gap:14px;margin-bottom:30px;}
  .botgrphd{display:flex;justify-content:space-between;align-items:baseline;gap:14px;flex-wrap:wrap;
    padding:7px 14px;background:var(--surface-2);border-bottom:1px solid var(--line);}
  .botgrphd .gsum{font-family:var(--mono);font-size:11.5px;color:var(--muted);
    font-variant-numeric:tabular-nums;}
  .botdet .label{display:block;margin-bottom:7px;}
  /* watching list: must WRAP inside the card. The string has no spaces and '·' is not a
     default break point, so without these rules it overflows the card's right edge (Bug 2). */
  .botdet .watching{display:flex;flex-wrap:wrap;gap:4px 6px;max-width:100%;overflow-wrap:anywhere;
    font-family:var(--mono);font-size:11px;line-height:1.5;}
  .botdet .wc{display:inline-flex;align-items:center;gap:3px;padding:1px 6px;border:1px solid var(--line);
    border-radius:5px;background:var(--surface);white-space:nowrap;}
  .botdet .wc.on{color:var(--teal);border-color:rgba(63,199,168,.4);}
  .botdet .wc .dot{width:5px;height:5px;border-radius:50%;background:var(--teal);}
  .botdet .wsep{display:none;}

  /* ===== square/rectangular-tile bot layout (replaces table rows) ===== */
  .tilegrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));
    align-items:start;          /* each tile sizes to its own content; expanded tile
                                   does NOT stretch its row-mates (row-expand bug fix) */
    overflow:visible;           /* expanded detail must never be clipped */
    padding:12px 14px;}
  @media(max-width:560px){.tilegrid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));}}
  .tile{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:11px 14px 10px;cursor:pointer;transition:border-color .12s ease,transform .12s ease,box-shadow .12s ease;
    display:flex;flex-direction:column;gap:8px;min-height:118px;align-self:start;overflow:visible;outline:none;}
  .tile[aria-expanded="true"]{z-index:2;}   /* expanded detail paints above row-mates */
  .tile:hover{border-color:var(--muted);transform:translateY(-1px);}
  .tile:focus-visible{outline:2px solid var(--amber);outline-offset:-2px;}
  .tile.up{border-left:3px solid var(--teal);}
  .tile.down{border-left:3px solid var(--brick);}
  .tile.flat{border-left:3px solid var(--muted);}
  .tile.off{opacity:.62;}
  .tile .thead{display:flex;align-items:center;gap:7px;}
  .tile .sdot{width:9px;height:9px;border-radius:50%;background:var(--faint);flex:none;}
  .tile.on .sdot{background:var(--teal);}
  .tile .tcat{margin-left:auto;font-family:var(--mono);font-size:8.5px;letter-spacing:.06em;
    text-transform:uppercase;color:var(--faint);border:1px solid var(--line);border-radius:5px;
    padding:1px 5px;white-space:nowrap;}
  .tile .tname{font-weight:700;font-size:14px;letter-spacing:-.01em;line-height:1.1;}
  .tile .tdesc{font-family:var(--mono);font-size:10px;color:var(--muted);line-height:1.3;
    max-height:26px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
  .tile .opct{display:inline-flex;align-items:center;font-family:var(--mono);font-size:9px;
    color:var(--amber);border:1px solid rgba(240,168,60,.35);border-radius:4px;padding:0 4px;margin:0;
    white-space:nowrap;}
  .tile .tmetrics{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;margin-top:2px;}
  .tile .tm{display:flex;flex-direction:column;gap:1px;}
  .tile .tm.full{grid-column:1 / -1;}
  .tile .tm .k{font-family:var(--mono);font-size:8.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);}
  .tile .tm .v{font-family:var(--mono);font-size:13.5px;font-weight:700;font-variant-numeric:tabular-nums;}
  .tile .tm .v.sm{font-size:11.5px;font-weight:600;}
  .tile .tspark{margin-top:auto;}
  .tile .tspark svg{min-width:100%;}
  .tile .tfoot{display:flex;align-items:center;justify-content:space-between;gap:8px;}
  /* ---- draggable tiles ---- */
  .tile{cursor:default;}
  .tile .thead{cursor:pointer;}                 /* click header to expand/collapse */
  .tile .grip{display:inline-flex;align-items:center;justify-content:center;width:14px;height:16px;
    margin-right:1px;flex:none;color:var(--faint);cursor:grab;opacity:.5;transition:opacity .12s,color .12s;
    border-radius:4px;user-select:none;-webkit-user-select:none;touch-action:none;}
  .tile .grip:hover{opacity:1;color:var(--muted);background:var(--surface-2);}
  .tile .grip:active{cursor:grabbing;}
  /* While dragging: the tile is lifted OUT of grid flow (position:fixed) so its on-screen
     anchor never moves — only a placeholder slot is shuffled, and siblings FLIP around it.
     This is what removes the jump/flicker of the old in-flow re-insert approach. */
  .tile.dragging{position:fixed;margin:0;z-index:1000;pointer-events:none;
    box-shadow:0 16px 40px rgba(0,0,0,.35);transition:none;}
  .tile-placeholder{border:1px dashed var(--line);border-radius:12px;background:var(--surface-2);
    opacity:.45;transition:none;}
  .tilegrid.dragging-on{cursor:grabbing !important;}
  .tilegrid.dragging-on .tile:not(.dragging){will-change:transform;}
  @media(prefers-reduced-motion:reduce){.tile{transition:none;}}
  .tile .tchev{color:var(--faint);font-size:13px;transition:transform .15s ease;}
  .tile[aria-expanded="true"] .tchev{transform:rotate(90deg);}
  .tile.detwrap{display:block;}
  /* detail is an inset panel INSIDE the tile — never a full-width top border that
     can be mistaken for the card edge. Subtle fill + rounded box makes containment obvious. */
  .botdet{margin-top:10px;background:var(--surface-2);border:1px solid var(--line);
    border-radius:9px;padding:11px 12px 12px;}
  .winbar{height:2px;border-radius:2px;background:var(--line);margin:3px 0 0 auto;max-width:46px;overflow:hidden;}
  .winbar>i{display:block;height:100%;background:var(--teal);border-radius:2px;}
  .pos-row{font-variant-numeric:tabular-nums;}
  .pos-row .dot{width:6px;height:6px;border-radius:50%;display:inline-block;margin-right:5px;vertical-align:middle;}

  .board{display:grid;grid-template-columns:repeat(6,1fr);gap:9px;}
  .coin{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 12px;}
  .coin.hold{border-left:3px solid var(--teal);}
  .coin.cash{border-left:3px solid var(--brick);}
  .coin .c{font-family:var(--mono);font-weight:700;font-size:14px;display:flex;align-items:center;gap:6px;}
  .coin .c .s{width:8px;height:8px;border-radius:50%;}
  .coin .g{font-family:var(--mono);font-size:11.5px;margin-top:5px;}
  .brakehead{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;}
  .chartcard{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:12px;}
  #pricechart{width:100%;height:600px;}
  #tvchart{width:100%;height:600px;}
  .charttabs{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;}
  .ctab{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:7px 13px;cursor:pointer;font:inherit;font-size:13px;transition:border-color .15s,color .15s;}
  .ctab:hover{color:var(--amber);}
  .ctab[aria-pressed="true"]{color:var(--teal);border-color:rgba(63,199,168,.5);background:rgba(63,199,168,.10);}
  .obrow{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:26px;}
  @media(max-width:900px){.obrow{grid-template-columns:1fr;}}
  .obcard{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font:12px ui-monospace,monospace;min-height:120px;}
  .obhead{display:grid;grid-template-columns:1.1fr 1fr 1fr;color:var(--faint);font-size:11px;padding-bottom:6px;border-bottom:1px solid var(--line);margin-bottom:4px;}
  .obhead span:nth-child(2),.obhead span:nth-child(3){text-align:right;}
  .obln{display:grid;grid-template-columns:1.1fr 1fr 1fr;padding:2px 0;position:relative;}
  .obln>span{position:relative;z-index:1;}
  .obln span:nth-child(3),.obln span:nth-child(4){text-align:right;}
  .obln .depth{position:absolute;top:0;bottom:0;right:0;z-index:0;opacity:.13;border-radius:2px;}
  .ob-ask{color:var(--brick);} .ob-bid{color:var(--teal);}
  .obmid{text-align:center;padding:7px 0;font-size:15px;font-weight:700;border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin:4px 0;font-variant-numeric:tabular-nums;}
  .chartcard.fs{background:var(--surface);padding:14px 18px 20px;overflow:auto;}
  .chartcard.fs #tvchart{height:calc(100vh - 150px)!important;}
  .chartcard.fs #pricechart{height:calc(100vh - 210px)!important;}
  .obtab{background:transparent;color:var(--faint);border:1px solid var(--line);border-radius:6px;padding:3px 9px;cursor:pointer;font:inherit;font-size:11px;}
  .obtab[aria-pressed="true"]{color:var(--teal);border-color:rgba(63,199,168,.5);}
  #depthcard{padding:10px 12px;}
  #depthchart{width:100%;height:300px;display:block;}
  .chartsel{font-family:var(--mono);font-size:12.5px;font-weight:600;background:var(--surface-2);
    color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 11px;cursor:pointer;}
  .chartsel:focus{outline:2px solid var(--amber);}
  .chartbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;}
  .chartstatus{font-family:var(--mono);font-size:13.5px;font-weight:700;}
  .rangebtns{display:flex;gap:4px;}
  .rbtn{font-family:var(--mono);font-size:11.5px;font-weight:600;padding:5px 10px;border-radius:7px;
    cursor:pointer;background:transparent;color:var(--muted);border:1px solid var(--line);}
  .rbtn:hover{color:var(--text);border-color:var(--faint);}
  .rbtn[aria-pressed="true"]{background:rgba(240,168,60,.14);color:var(--amber);border-color:var(--amber);}
  .chartlegend{display:flex;flex-wrap:wrap;gap:13px;margin-bottom:9px;font-family:var(--mono);font-size:11px;color:var(--muted);}
  .chartlegend i.lg{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:middle;margin-right:5px;}
  .charttip{position:absolute;z-index:6;pointer-events:none;background:var(--surface-2);
    border:1px solid var(--line);border-radius:9px;padding:9px 12px;font-family:var(--mono);
    font-size:11.5px;min-width:160px;box-shadow:0 8px 24px rgba(0,0,0,.5);}
  .charttip .tt-date{font-weight:700;color:var(--text);border-bottom:1px solid var(--line);padding-bottom:5px;margin-bottom:6px;}
  .charttip .tt-row{display:flex;justify-content:space-between;gap:16px;margin:2px 0;color:var(--muted);}
  .charttip .tt-row b{color:var(--text);font-weight:700;font-variant-numeric:tabular-nums;}
  .charttip .tt-status{margin-top:6px;padding-top:5px;border-top:1px solid var(--line);font-weight:700;}
  .pfcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .pfhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;}
  .pfhead .dep{font-family:ui-monospace,Menlo,monospace;font-size:20px;font-weight:700;}
  .pfbar{height:8px;border-radius:6px;background:var(--line);overflow:hidden;margin-bottom:12px;}
  .pfbar>i{display:block;height:100%;background:var(--teal);}
  .pfrow{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--line);font-size:13.5px;}
  .pfrow .nm{flex:1;font-weight:600;}
  .pfrow .st{font-family:ui-monospace,Menlo,monospace;font-size:12px;}
  .feed{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 0;
        max-height:280px;overflow-y:auto;}
  .feed .ev{display:flex;gap:9px;padding:7px 14px;border-top:1px solid var(--line);font-size:13px;}
  .feed .ev:first-child{border-top:none;}
  .feed .src{font-family:ui-monospace,Menlo,monospace;font-size:10px;letter-spacing:.04em;
        padding:2px 6px;border-radius:5px;height:fit-content;background:var(--line);color:var(--muted);text-transform:uppercase;}
  .feed .evtxt{flex:1;min-width:0;}
  .feed .evtxt .t1{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .feed .evtxt .tm{color:var(--faint);font-size:11px;}
  @media(max-width:640px){.pfcard,.feed{margin-bottom:4px;}}

  .fund{display:flex;flex-wrap:wrap;gap:16px 30px;align-items:center;background:var(--surface);
    border:1px solid var(--line);border-radius:13px;padding:18px 20px;margin-top:13px;}
  .fund .verdict{font-family:var(--mono);font-size:13px;font-weight:700;letter-spacing:.06em;
    padding:6px 13px;border-radius:20px;border:1px solid var(--line);}
  .fund .verdict.skip{color:var(--faint);}
  .fund .verdict.modest{color:var(--amber);border-color:rgba(240,168,60,.4);background:rgba(240,168,60,.08);}
  .fund .verdict.strong{color:var(--teal);border-color:rgba(63,199,168,.4);background:rgba(63,199,168,.08);}
  .fund .fm{display:flex;flex-direction:column;gap:3px;}
  .fund .fm .v{font-family:var(--mono);font-size:19px;font-weight:700;}
  .fund .hint{font-family:var(--mono);font-size:11.5px;color:var(--muted);max-width:34ch;margin-left:auto;}

  .mem{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:18px 20px;margin-top:13px;}
  .mem .row1{display:flex;flex-wrap:wrap;gap:14px 30px;align-items:center;}
  .mem .m{display:flex;flex-direction:column;gap:3px;}
  .mem .m .v{font-family:var(--mono);font-size:19px;font-weight:700;}
  .mem .none{font-family:var(--mono);font-size:13px;color:var(--faint);}
  .mem .holds{border-top:1px solid var(--line);margin-top:15px;padding-top:13px;display:flex;flex-direction:column;gap:7px;}
  .mem .hrow{display:flex;justify-content:space-between;gap:10px;font-family:var(--mono);font-size:12.5px;}
  .mem .hrow .p{color:var(--muted);}

  .eqwrap{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:16px 18px;}
  .eqleg{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:12px;}
  .eqleg span{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11.5px;color:var(--muted);}
  .eqleg i{width:14px;height:3px;border-radius:2px;display:inline-block;}
  canvas#eqchart{width:100%;height:260px;display:block;}
  .eqempty{font-family:var(--mono);font-size:13px;color:var(--faint);padding:34px 0;text-align:center;}

  .err{font-family:var(--mono);font-size:13px;color:var(--brick);padding:16px 0;}

  /* ---- backtest lab panel ---- */
  .btgrid{display:grid;grid-template-columns:1fr;gap:20px;}
  .btcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0;overflow:hidden;}
  .btcard .bthead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--line);}
  .btcard .bthead h3{font-size:16px;font-weight:700;margin:0;}
  .btcard .btmeta{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-bottom:8px;}
  .btcard .btmeta span{margin-right:8px;}
  .bttable-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
  .bttable{width:100%;border-collapse:collapse;font-size:12.5px;table-layout:fixed;}
  .bttable th{font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--muted);text-align:right;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap;}
  .bttable th:first-child{text-align:left;width:32%;}
  .bttable th:nth-child(n+2){width:13.6%;}
  .bttable td{padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;
    text-align:right;font-family:ui-monospace,Menlo,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .bttable td:first-child{text-align:left;font-family:inherit;font-weight:600;
    overflow:hidden;text-overflow:ellipsis;white-space:normal;overflow-wrap:break-word;}
  .bttable tr:last-child td{border-bottom:none;}
  .bttable tbody tr:nth-child(even){background:var(--surface-2);}
  .bttable .pos{color:var(--teal);}
  .bttable .neg{color:var(--brick);}
  .bttable .bench{color:var(--muted);font-style:italic;}
  .bttable .inf{color:var(--faint);}

  .bttable td.bench{color:var(--faint);font-style:italic;}

  /* ---- backtest analyst panel ---- */
  #analyses{display:grid;grid-template-columns:1fr;gap:20px;}
  .analyst-summary{font-size:14px;line-height:1.5;margin:8px 0;padding:10px 12px;
    background:var(--bg);border-radius:8px;border-left:3px solid var(--teal);}
  .analyst-section{font-size:13px;line-height:1.5;margin:6px 0;color:var(--text);}
  .analyst-section b{color:var(--faint);font-weight:600;}
  .analyst-list{margin:6px 0 0;padding-left:18px;font-size:12.5px;line-height:1.6;}
  .analyst-list li{margin-bottom:4px;}
  .analyst-list li b{color:var(--text);}

  /* narrow screens: shed the least-load-bearing columns (sparkline, trades, win)
     rather than let the row wrap — balance and P&L are the two that must survive */
  @media(max-width:900px){.board{grid-template-columns:repeat(3,1fr);}
    .botcols{grid-template-columns:10px minmax(110px,2fr) 88px 92px 74px 12px;}
    .botcols .c-spark,.botcols .c-trades,.botcols .c-win{display:none;}
    .totals{gap:22px;}.totals .big{font-size:21px;}
    .btgrid{grid-template-columns:1fr;}}
  @media(max-width:520px){.board{grid-template-columns:repeat(2,1fr);}}

  /* ---- Polymarket copy-trading bot section ---- */
  .polyhead{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-top:26px;}
  .polystats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-top:18px;}
  .polystat{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .polystat .lb{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;}
  .polystat .vl{font-size:22px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums;}
  .polystat .sb{font-size:11px;color:var(--muted);margin-top:2px;}
  .polygrid{display:grid;grid-template-columns:1.4fr 1fr;gap:20px;margin-top:18px;}
  .polycard{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0;}
  .polycard h3{font-size:13px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:.03em;}
  .polytable{width:100%;border-collapse:collapse;font-size:12.5px;}
  .polytbl{width:100%;border-collapse:collapse;font-size:12.5px;}
  .polytbl th,.polytable th{font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;
    text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);font-weight:600;}
  .polytable td{padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;}
  .polytbl td{padding:7px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;}
  .polytable tr:last-child td{border-bottom:none;}
  .polytbl tr:last-child td{border-bottom:none;}
  .polyedge{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:6px;
    background:var(--surface-2);color:var(--muted);margin:1px 2px 1px 0;border:1px solid var(--line);}
  .polyedge.hot{background:rgba(63,199,168,.12);color:var(--teal);border-color:rgba(63,199,168,.35);}
  .polybar{height:5px;border-radius:3px;background:var(--surface-2);overflow:hidden;min-width:44px;}
  .polybar i{display:block;height:100%;background:var(--teal);border-radius:3px;}
  .polypos{font-size:12.5px;padding:9px 0;border-bottom:1px solid var(--line);display:flex;
    justify-content:space-between;gap:10px;align-items:baseline;}
  .polypos:last-child{border-bottom:none;}
  .polypos .mk{color:var(--text);font-weight:600;}
  .polypos .sd{color:var(--muted);font-size:11px;}
  .polyrule{font-size:12px;padding:8px 0;border-bottom:1px solid var(--line);color:var(--muted);}
  .polyrule:last-child{border-bottom:none;}
  .polyrule b{color:var(--text);}
  .polyempty{color:var(--faint);font-size:12.5px;padding:14px 0;text-align:center;}
  .polymix span{display:inline-block;margin:2px 4px 2px 0;font-size:11px;padding:2px 8px;border-radius:6px;
    background:var(--surface-2);border:1px solid var(--line);color:var(--muted);}
  .polycanvas{width:100%;height:170px;}
  @media(max-width:1100px){.polystats{grid-template-columns:repeat(2,1fr);}.polygrid{grid-template-columns:1fr;}}

  /* ---- sectioned layout: sticky menubar + tab pages ---- */
  .menubar{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:4px;flex-wrap:wrap;
    background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:6px 8px;margin-top:18px;box-shadow:0 6px 18px rgba(0,0,0,.25);}
  .menubar .mtab{appearance:none;border:1px solid transparent;background:transparent;color:var(--muted);
    font:600 12.5px var(--sans);padding:7px 13px;border-radius:8px;cursor:pointer;transition:all .15s;}
  .menubar .mtab:hover{color:var(--text);background:var(--surface-2);}
  .menubar .mtab.active{color:var(--teal);background:rgba(63,199,168,.1);border-color:rgba(63,199,168,.35);}
  .menubar .mright{margin-left:auto;display:flex;gap:4px;align-items:center;flex-wrap:wrap;}
  .menubar .tbtn{font-size:12px;padding:6px 10px;}
  .tabpage{display:none;}
  .tabpage.active{display:block;animation:fadein .18s ease;}
  @keyframes fadein{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
  @media(max-width:720px){.menubar{position:static;}.menubar .mright{margin-left:0;width:100%;justify-content:flex-end;}}
</style></head>
<body>
<div class="wrap">
<header>
    <div class="brand">
      <svg class="glyph" viewBox="0 0 32 32" fill="none" aria-hidden="true">
        <circle cx="16" cy="16" r="14" stroke="#26314A" stroke-width="2"/>
        <path d="M16 4 A12 12 0 0 1 28 16" stroke="#F0A83C" stroke-width="3" stroke-linecap="round"/>
        <circle cx="16" cy="16" r="3.2" fill="#F0A83C"/>
        <path d="M16 16 L23 9" stroke="#E6EAF2" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <div><h1>Trading Desk</h1><small>all bots · one screen</small></div>
    </div>
    <div class="totals">
      <div class="t"><span class="label">Combined equity</span>
        <span class="big" id="tbal">—</span></div>
      <div class="t"><span class="label">Total P&L</span>
        <span class="big" id="tpnl">—</span>
        <div id="tpnl_sub" class="mono" style="font-size:11px;margin-top:2px;color:var(--muted)"></div></div>
      <div class="t"><span class="label">Status</span>
        <span class="live"><i class="dot pulse" id="livedot"></i><span id="upd">connecting…</span></span></div>
    </div>
  </header>

  <nav class="menubar" aria-label="Sections">
    <button class="mtab" data-tab="desk" type="button">📊 Desk</button>
    <button class="mtab" data-tab="charts" type="button">📈 Charts</button>
    <button class="mtab" data-tab="brake" type="button">🪂 Brake board</button>
    <button class="mtab" data-tab="backtests" type="button">🧪 Backtests</button>
    <button class="mtab" data-tab="polymarket" type="button">🎯 Polymarket</button>
    <button class="mtab" data-tab="india" type="button">🇮🇳 India</button>
    <button class="mtab" data-tab="nifty" type="button">🪙 Nifty 5m</button>
    <span class="mright">
      <button class="tbtn" id="btnBrake" type="button">🪂 Brake — live check</button>
      <button class="tbtn" id="btnMem" type="button">🧠 Memory — full record</button>
      <button class="tbtn" id="themeToggle" type="button">☀️ Light mode</button>
    </span>
  </nav>

  <div id="guard" class="guard" hidden></div>

  <section class="tabpage" data-tab="desk">
  <h2 class="sec">Bots</h2>
  <div class="bots" id="bots"></div>

  <div class="panelrow" style="margin-top:26px">
    <div>
      <div class="brakehead"><h2 class="sec">Diversified braked portfolio</h2>
        <span class="label" id="pfsum"></span></div>
      <div class="pfcard" id="portfolio"></div>
    </div>
    <div>
      <div class="brakehead"><h2 class="sec">Activity · same as Telegram</h2>
        <span class="label" style="color:var(--faint)">newest first</span></div>
      <div class="feed" id="activity"></div>
    </div>
  </div>

  <div class="pfcard" style="margin-top:26px">
    <div class="brakehead" style="margin-top:0">
      <h2 class="sec">Nifty 5m paper bot</h2>
      <span class="label" style="color:var(--faint)" id="niftyLast">paper · no orders · 2% risk</span>
    </div>
    <div id="niftyDesk"><div style="color:var(--faint);padding:8px 0">loading…</div></div>
  </div>
  </section>

  <section class="tabpage" data-tab="charts">

  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec" id="chartToggle" style="cursor:pointer" title="click to collapse/expand">▾ Price chart</h2>
    <select id="chartAsset" class="chartsel" aria-label="Choose asset"></select>
  </div>
  <div class="chartcard" id="chartCard">
    <div class="charttabs">
      <button class="ctab" id="tabTrader" type="button" aria-pressed="true">📈 Trader · TradingView</button>
      <button class="ctab" id="tabBot" type="button" aria-pressed="false">🤖 Bot · brake + signals</button>
      <button class="ctab" id="chartFull" type="button" style="margin-left:auto">⛶ Fullscreen</button>
    </div>

    <div id="traderview">
      <div id="tvchart"></div>
      <div class="chartlegend" style="color:var(--faint)">
        Full TradingView charting — drawing tools, indicators &amp; every timeframe, exactly like ApeX.
        Add your 200-day brake line via <b>Indicators → Moving Average</b> (set length&nbsp;200). Data: TradingView.
      </div>
    </div>

    <div id="botview" style="display:none">
      <div class="chartbar">
        <span class="chartstatus" id="chartStatus">—</span>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="rangebtns" id="rangeBtns"></div>
          <button class="tbtn" id="chartExpand" type="button" style="padding:6px 11px">⤢ Expand</button>
        </div>
      </div>
      <div class="chartlegend">
        <span><i class="lg" style="background:#3FC7A8"></i>up day</span>
        <span><i class="lg" style="background:#E5674E"></i>down day</span>
        <span><i class="lg" style="background:#F0A83C;height:3px"></i>200-day line</span>
        <span><i class="lg" style="background:#3FC7A8;border-radius:50%"></i>▲ buy signal</span>
        <span><i class="lg" style="background:#E5674E;border-radius:50%"></i>▼ sell signal</span>
        <span style="color:var(--faint)">· hover a candle for details</span>
      </div>
      <div id="pricechart"></div>
      <div class="eqempty" id="chartmsg" style="display:none"></div>
    </div>
  </div>

  <div class="obrow">
    <div>
      <div class="brakehead">
        <h2 class="sec">Order book</h2>
        <span style="display:flex;gap:6px;align-items:center">
          <span class="label" id="obsum"></span>
          <button class="obtab" id="obLadder" type="button" aria-pressed="true">Ladder</button>
          <button class="obtab" id="obDepth" type="button" aria-pressed="false">Depth</button>
        </span>
      </div>
      <div class="obcard" id="orderbook"><div style="color:var(--faint)">loading…</div></div>
      <div class="obcard" id="depthcard" style="display:none"><canvas id="depthchart"></canvas></div>
    </div>
    <div>
      <div class="brakehead"><h2 class="sec">Recent trades</h2><span class="label" id="trsum"></span></div>
      <div class="obcard" id="recenttrades"><div style="color:var(--faint)">loading…</div></div>
    </div>
  </div>
  </section>

  <section class="tabpage" data-tab="brake">
  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">200-day brake board · crypto</h2>
    <span class="label" id="brakesum"></span>
  </div>
  <div class="board" id="board"></div>

  <div class="panelrow">
    <div>
      <h2 class="sec">Equity vs buy &amp; hold</h2>
      <div class="eqwrap">
        <div class="eqleg">
          <span><i style="background:var(--amber)"></i>Braked Hold</span>
          <span><i style="background:#6BA5E0"></i>Spot</span>
          <span><i style="background:#A98BE0"></i>Futures</span>
          <span><i style="background:#4ADE80"></i>S&amp;P 500</span>
          <span><i style="background:#FF7AB6"></i>Nifty 50</span>
          <span><i style="background:#FFB347"></i>ONGC</span>
          <span><i style="background:#9ACD32"></i>ITC</span>
          <span><i style="background:#F7931A"></i>BTC</span>
          <span><i style="background:var(--brick)"></i>BTC hold</span>
          <span><i style="background:var(--teal)"></i>Basket hold</span>
        </div>
        <canvas id="eqchart"></canvas>
        <div class="eqempty" id="eqempty" style="display:none"></div>
      </div>
    </div>
    <div>
      <h2 class="sec">Brake memory · track record</h2>
      <div class="mem" id="memory"></div>
    </div>
  </div>
  </section>

  <section class="tabpage" data-tab="backtests">
  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">Backtest lab · after-tax research</h2>
    <span class="label" style="color:var(--faint)">offline · static results</span>
  </div>
  <div class="btgrid" id="backtests"></div>

  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">Backtest analyst · LLM reports</h2>
    <span class="label" style="color:var(--faint)">AI-generated · for human review</span>
  </div>
  <div id="analyses"></div>
  </section>

  <section class="tabpage" data-tab="polymarket">
  <div class="polyhead">
    <h2 class="sec" style="margin-top:0">Polymarket copy-trading bot</h2>
    <span class="label" style="color:var(--faint)" id="polylast">paper · local</span>
  </div>
  <div class="polystats" id="polystats">
    <div class="polystat"><div class="lb">Paper equity</div><div class="vl" id="pEq">—</div><div class="sb">starting bankroll $1,000</div></div>
    <div class="polystat"><div class="lb">Net P&amp;L</div><div class="vl" id="pPnl">—</div><div class="sb" id="pPnlSub"></div></div>
    <div class="polystat"><div class="lb">Wallet universe</div><div class="vl" id="pWallets">—</div><div class="sb" id="pWalletsSub"></div></div>
    <div class="polystat"><div class="lb">Open positions</div><div class="vl" id="pPos">—</div><div class="sb" id="pPosSub"></div></div>
  </div>
  <div class="polygrid">
    <div class="polycard">
      <h3>Equity curve · paper</h3>
      <canvas class="polycanvas" id="polyChart"></canvas>
      <div class="polyempty" id="polyChartEmpty" style="display:none">no hourly snapshots yet</div>
    </div>
    <div class="polycard">
      <h3>Decision mix</h3>
      <div class="polymix" id="polyMix"><span class="polyempty" style="display:block;text-align:left;padding:0">no decisions yet</span></div>
      <h3 style="margin-top:14px">Rule versions</h3>
      <div id="polyRules"><div class="polyempty">no rule updates yet</div></div>
    </div>
  </div>
  <div class="polygrid">
    <div class="polycard">
      <h3>Top wallets · score &amp; category edge</h3>
      <table class="polytable" id="polyWallets"><tbody><tr><td class="polyempty">no wallet scores yet</td></tr></tbody></table>
    </div>
    <div class="polycard">
      <h3>Paper positions</h3>
      <div id="polyPositions"><div class="polyempty">no positions yet</div></div>
    </div>
  </div>
  </section>

  <section class="tabpage" data-tab="india">
  <div class="polyhead">
    <h2 class="sec" style="margin-top:0">India Desk · Indian markets</h2>
    <span class="label" style="color:var(--faint)" id="indiaLast">data-only · no orders</span>
  </div>
  <div class="polyempty" id="indiaNote" style="margin-bottom:14px;color:var(--amber)">
    ⚠️ India's MeitY blocked Polymarket (Apr 2026) and warned VPN providers.
    This desk only <b>reads public market data</b> — it never places orders.
  </div>
  <div class="polystats">
    <div class="polystat"><span class="label">Indian markets</span><b id="indiaCount">—</b></div>
    <div class="polystat"><span class="label">Whale trades · 7d</span><b id="indiaTrades">—</b></div>
    <div class="polystat"><span class="label">Watchlist</span><b id="indiaWatch">6</b></div>
  </div>
  <div class="polycard" style="margin-top:16px">
    <h3>Indian markets · live prices & whale flow</h3>
    <div id="indiaMarkets"><div class="polyempty">loading…</div></div>
  </div>
  <div class="polycard" style="margin-top:16px">
    <h3>Recent whale trades · India-relevant</h3>
    <div id="indiaTradesTable"><div class="polyempty">loading…</div></div>
  </div>
  </section>

  <section class="tabpage" data-tab="nifty">
  <div style="height:calc(100vh - 160px);border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface)">
    <iframe src="/nifty/" style="width:100%;height:100%;border:0;"></iframe>
  </div>
  </section>

  <div class="err" id="err" hidden></div>
</div>

<div class="modal" id="modal">
  <div class="modal-content">
    <div class="modal-head"><h3 id="modalTitle"></h3>
      <button class="modal-close" id="modalClose" type="button" aria-label="Close">✕</button></div>
    <div id="modalBody"></div>
  </div>
</div>

<script src="/static/lightweight-charts.js"></script>
<script>
const $=id=>document.getElementById(id);
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
document.documentElement.setAttribute("data-theme", localStorage.getItem("deskTheme")||"dark");

// ---- stale-tab self-heal (no reload loop) ----
// The server 307-redirects "/" to "/d/<build>/" so every deploy changes the URL a frozen
// bfcache tab (running old JS that ignores no-store) lands on. This client check handles the
// case where a tab is frozen at an OLD "/d/<oldbuild>/" path: it moves to the current build
// path ONCE via location.replace (no reload loop). We record the build only AFTER confirming
// we're on the right path, so we never reload/replace twice.
const DASH_BUILD="__DASH_BUILD__";
try{
  const wantPath="/d/"+DASH_BUILD+"/";
  if(location.pathname!==wantPath){ location.replace(wantPath); }
  else { sessionStorage.setItem("dashBuild", DASH_BUILD); }
}catch(e){ /* sessionStorage/location unavailable: skip guard */ }

// ---- price chart (candles + volume + 200-day line + buy/sell markers) ----
const CHART_ASSETS=[
  {g:"Crypto",items:["BTC","ETH","SOL","XRP","ADA","LTC","DOGE","LINK","BNB","AVAX","DOT","TRX"]},
  {g:"Macro",items:[["SPY","S&P 500"],["QQQ","Nasdaq 100"],["^NSEI","NIFTY 50"],["TLT","US Bonds"],["GLD","Gold"],["PPLT","Platinum"]]},
];
function fmtTime(t){
  if(typeof t==="string") return t;
  if(t&&t.year) return `${t.year}-${String(t.month).padStart(2,"0")}-${String(t.day).padStart(2,"0")}`;
  return String(t);
}
let _chart=null,_cs=null,_ss=null,_vs=null,_data=null,_range="1Y",_tall=false,_lastEquity=null;
function initChart(){
  if(_chart||!window.LightweightCharts) return;
  const el=$("pricechart");
  _chart=LightweightCharts.createChart(el,{
    height:600,
    layout:{background:{color:css("--surface")},textColor:css("--muted"),fontFamily:"ui-monospace,monospace"},
    grid:{vertLines:{color:css("--line")},horzLines:{color:css("--line")}},
    timeScale:{borderColor:css("--line")},rightPriceScale:{borderColor:css("--line")},
    crosshair:{mode:0},
  });
  _cs=_chart.addCandlestickSeries({upColor:"#3FC7A8",downColor:"#E5674E",borderVisible:false,
    wickUpColor:"#3FC7A8",wickDownColor:"#E5674E"});
  _ss=_chart.addLineSeries({color:"#F0A83C",lineWidth:2,priceLineVisible:false,lastValueVisible:false,title:"200d"});
  _vs=_chart.addHistogramSeries({priceFormat:{type:"volume"},priceScaleId:""});
  _vs.priceScale().applyOptions({scaleMargins:{top:0.84,bottom:0}});
  el.style.position="relative";
  const tip=document.createElement("div"); tip.className="charttip"; tip.style.display="none"; el.appendChild(tip);
  _chart.subscribeCrosshairMove(param=>{
    if(!param.time||!param.point||param.point.x<0||param.point.y<0){ tip.style.display="none"; return; }
    const c=param.seriesData.get(_cs);
    if(!c){ tip.style.display="none"; return; }
    const smaV=(param.seriesData.get(_ss)||{}).value;
    const volV=(param.seriesData.get(_vs)||{}).value;
    const above=smaV!=null && c.close>=smaV, chg=(c.close/c.open-1)*100;
    const vol=volV!=null?Number(volV).toLocaleString(undefined,{maximumFractionDigits:0}):"—";
    tip.innerHTML=`<div class="tt-date">${fmtTime(param.time)}</div>`
      +`<div class="tt-row"><span>open</span><b>${price(c.open)}</b></div>`
      +`<div class="tt-row"><span>high</span><b>${price(c.high)}</b></div>`
      +`<div class="tt-row"><span>low</span><b>${price(c.low)}</b></div>`
      +`<div class="tt-row"><span>close</span><b>${price(c.close)}</b></div>`
      +`<div class="tt-row"><span>day</span><b class="${chg>=0?'pos':'neg'}">${chg>=0?'+':''}${chg.toFixed(2)}%</b></div>`
      +(smaV!=null?`<div class="tt-row"><span>200-day</span><b style="color:var(--amber)">${price(smaV)}</b></div>`:"")
      +`<div class="tt-row"><span>volume</span><b>${vol}</b></div>`
      +(smaV!=null?`<div class="tt-status" style="color:${above?'var(--teal)':'var(--brick)'}">${above?'🟢 HOLD · above line':'🔴 CASH · below line'}</div>`:"");
    tip.style.display="block";
    const w=el.clientWidth,h=el.clientHeight,tw=tip.offsetWidth,th=tip.offsetHeight;
    let x=param.point.x+18,y=param.point.y+18;
    if(x+tw>w) x=param.point.x-tw-18;
    if(y+th>h) y=h-th-6;
    if(x<0) x=6;
    tip.style.left=x+"px"; tip.style.top=y+"px";
  });
  new ResizeObserver(()=>{ if(_chart) _chart.applyOptions({width:el.clientWidth}); }).observe(el);
}
function markersFrom(candles,sma){
  const m={}; sma.forEach(s=>m[s.time]=s.value); const out=[];
  for(let i=1;i<candles.length;i++){
    const t=candles[i].time, tp=candles[i-1].time;
    if(m[t]==null||m[tp]==null) continue;
    const above=candles[i].close>m[t], was=candles[i-1].close>m[tp];
    if(above&&!was) out.push({time:t,position:"belowBar",color:"#3FC7A8",shape:"arrowUp",text:"BUY"});
    else if(!above&&was) out.push({time:t,position:"aboveBar",color:"#E5674E",shape:"arrowDown",text:"SELL"});
  }
  return out;
}
function setChartStatus(label,candles,sma){
  const el=$("chartStatus"), last=candles[candles.length-1];
  const lastSma=sma.length?sma[sma.length-1].value:null;
  if(!last||!lastSma){ el.textContent=label; return; }
  const above=last.close>=lastSma, gap=(last.close/lastSma-1)*100;
  el.innerHTML=`${label} &nbsp;<span style="color:${above?'var(--teal)':'var(--brick)'}">`
    +`${above?'🟢 HOLD':'🔴 CASH'} ${gap>=0?'+':''}${gap.toFixed(0)}% vs 200-day line</span>`;
}
function applyRange(){
  if(!_chart||!_data) return;
  const n=_data.candles.length, bars={"3M":90,"6M":180,"1Y":365,"2Y":730,"ALL":n}[_range]||n;
  if(_range==="ALL") _chart.timeScale().fitContent();
  else _chart.timeScale().setVisibleLogicalRange({from:Math.max(0,n-bars),to:n});
}
async function loadChart(asset){
  initChart();
  const msg=$("chartmsg");
  if(!_chart){ msg.style.display="block"; msg.textContent="chart library didn't load"; return; }
  msg.style.display="block"; msg.textContent="loading "+asset+"…";
  try{
    const d=await (await fetch("/api/candles?asset="+encodeURIComponent(asset),{cache:"no-store"})).json();
    if(d.error){ msg.textContent="chart error: "+d.error; return; }
    _data=d;
    _cs.setData(d.candles.map(c=>({time:c.time,open:c.open,high:c.high,low:c.low,close:c.close})));
    _ss.setData(d.sma);
    _vs.setData(d.candles.map(c=>({time:c.time,value:c.volume||0,
      color:c.close>=c.open?"rgba(63,199,168,0.4)":"rgba(229,103,78,0.4)"})));
    _cs.setMarkers(markersFrom(d.candles,d.sma));
    setChartStatus(d.label||asset,d.candles,d.sma);
    applyRange();
    msg.style.display="none";
  }catch(e){ msg.textContent="chart error: "+e.message; }
}
function buildRangeBtns(){
  const el=$("rangeBtns"); if(!el||el.children.length) return;
  ["3M","6M","1Y","2Y","ALL"].forEach(r=>{
    const b=document.createElement("button"); b.className="rbtn"; b.type="button"; b.textContent=r;
    b.setAttribute("aria-pressed",r===_range);
    b.onclick=()=>{ _range=r; el.querySelectorAll(".rbtn").forEach(x=>x.setAttribute("aria-pressed",x.textContent===r)); applyRange(); };
    el.appendChild(b);
  });
}
function buildChartSelector(){
  const sel=$("chartAsset"); if(!sel||sel.children.length) return;
  for(const grp of CHART_ASSETS){
    const og=document.createElement("optgroup"); og.label=grp.g;
    for(const it of grp.items){
      const sym=Array.isArray(it)?it[0]:it, name=Array.isArray(it)?`${it[0]} · ${it[1]}`:it;
      const o=document.createElement("option"); o.value=sym; o.textContent=name; og.appendChild(o);
    }
    sel.appendChild(og);
  }
  sel.onchange=()=>{ loadChart(sel.value); if(_tvView==="trader") buildTV(tvSymbol(sel.value)); if(typeof pollMarket==="function") pollMarket(); };
}
$("chartExpand").onclick=()=>{
  _tall=!_tall; const h=_tall?Math.max(700,window.innerHeight-150):600;
  $("pricechart").style.height=h+"px"; if(_chart) _chart.applyOptions({height:h});
  $("chartExpand").textContent=_tall?"⤡ Shrink":"⤢ Expand"; applyRange();
  if(_tall) $("chartCard").scrollIntoView({behavior:"smooth",block:"start"});
};
$("chartToggle").onclick=()=>{
  const c=$("chartCard"), hidden=c.style.display==="none";
  c.style.display=hidden?"block":"none";
  $("chartToggle").textContent=(hidden?"▾":"▸")+" Price chart";
  if(hidden) setTimeout(()=>{ if(_chart){ _chart.applyOptions({width:$("pricechart").clientWidth}); applyRange(); } },60);
};
buildRangeBtns(); buildChartSelector(); loadChart("BTC");

// ---- Trader view: TradingView Advanced Chart (ApeX-style, full feature set) ----
// Same charting engine ApeX embeds — drawing tools, indicators, all timeframes. It's a
// self-contained widget on TradingView's data, so it can't draw OUR 200d line / trade
// markers (that's what the Bot view is for). Needs internet; degrades gracefully offline.
let _tvView="trader", _tvScript=null;
function tvSymbol(a){
  const M={SPY:"AMEX:SPY",QQQ:"NASDAQ:QQQ","^NSEI":"NSE:NIFTY",TLT:"NASDAQ:TLT",GLD:"AMEX:GLD",PPLT:"AMEX:PPLT"};
  return M[a]||("BINANCE:"+String(a||"BTC").toUpperCase()+"USDT");
}
function loadTV(){
  if(_tvScript) return _tvScript;
  _tvScript=new Promise((res,rej)=>{
    const s=document.createElement("script");
    s.src="https://s3.tradingview.com/tv.js"; s.async=true;
    s.onload=res; s.onerror=()=>rej(new Error("tv.js blocked or offline"));
    document.head.appendChild(s);
  });
  return _tvScript;
}
function buildTV(sym){
  const el=$("tvchart"); if(!el) return;
  loadTV().then(()=>{
    el.innerHTML="";
    new TradingView.widget({
      container_id:"tvchart", autosize:true, symbol:sym, interval:"D",
      timezone:"Etc/UTC",
      theme:document.documentElement.getAttribute("data-theme")==="light"?"light":"dark",
      style:"1", locale:"en", hide_side_toolbar:false, withdateranges:true,
      allow_symbol_change:true, details:true, backgroundColor:css("--surface"),
    });
  }).catch(e=>{
    el.innerHTML='<div class="eqempty" style="display:block">TradingView couldn\'t load ('
      +e.message+') — it needs internet. The <b>Bot</b> view works offline.</div>';
  });
}
function switchChartView(v){
  _tvView=v; const trader=(v==="trader");
  $("traderview").style.display=trader?"block":"none";
  $("botview").style.display=trader?"none":"block";
  $("tabTrader").setAttribute("aria-pressed",trader);
  $("tabBot").setAttribute("aria-pressed",!trader);
  if(trader) buildTV(tvSymbol($("chartAsset").value||"BTC"));
  else if(_chart){ _chart.applyOptions({width:$("pricechart").clientWidth,height:$("pricechart").clientHeight}); applyRange(); }
}
$("tabTrader").onclick=()=>switchChartView("trader");
$("tabBot").onclick=()=>switchChartView("bot");

// ---- true fullscreen for whichever chart view is active (works for both) ----
function fsEl(){ return document.fullscreenElement||document.webkitFullscreenElement; }
$("chartFull").onclick=()=>{
  const card=$("chartCard");
  if(fsEl()){ (document.exitFullscreen||document.webkitExitFullscreen).call(document); }
  else{ const rq=card.requestFullscreen||card.webkitRequestFullscreen; if(rq) rq.call(card); }
};
function onFsChange(){
  const card=$("chartCard"), fs=(fsEl()===card);
  card.classList.toggle("fs",fs);
  $("chartFull").textContent=fs?"⛶ Exit fullscreen":"⛶ Fullscreen";
  // let the CSS resize settle, then resize the active chart to fill (or restore)
  setTimeout(()=>{
    if(_tvView==="trader") buildTV(tvSymbol($("chartAsset").value||"BTC"));
    else if(_chart){ _chart.applyOptions({width:$("pricechart").clientWidth,height:$("pricechart").clientHeight}); applyRange(); }
  },130);
}
document.addEventListener("fullscreenchange",onFsChange);
document.addEventListener("webkitfullscreenchange",onFsChange);

// ---- live order book + recent trades (read-only, ApeX-style) ----
function renderOrderBook(d){
  const el=$("orderbook"), sum=$("obsum");
  if(!d||d.error){ el.innerHTML=`<div style="color:var(--faint)">${(d&&d.error)||"unavailable"}</div>`; sum.textContent=""; return; }
  const asks=d.asks||[], bids=d.bids||[];
  const maxv=Math.max(1e-9,...asks.map(a=>a[1]),...bids.map(b=>b[1]));
  const line=(p,v,side)=>{
    const w=(v/maxv*100).toFixed(1), col=side==="ask"?"var(--brick)":"var(--teal)";
    return `<div class="obln"><i class="depth" style="width:${w}%;background:${col}"></i>`
      +`<span class="ob-${side}">${price(p)}</span>`
      +`<span>${v.toFixed(4)}</span><span>${(p*v).toLocaleString(undefined,{maximumFractionDigits:0})}</span></div>`;};
  const bestAsk=asks[0]?asks[0][0]:0, bestBid=bids[0]?bids[0][0]:0;
  const mid=(bestAsk&&bestBid)?(bestAsk+bestBid)/2:(bestAsk||bestBid);
  let html=`<div class="obhead"><span>Price</span><span>Amount</span><span>Total $</span></div>`;
  html+=asks.slice().reverse().map(a=>line(a[0],a[1],"ask")).join("");   // lowest ask nearest mid
  html+=`<div class="obmid">${price(mid)}</div>`;
  html+=bids.map(b=>line(b[0],b[1],"bid")).join("");                     // highest bid nearest mid
  el.innerHTML=html;
  sum.textContent=d.source?`${d.pair} · ${d.source}`:"";
}
function renderTrades(d){
  const el=$("recenttrades"), sum=$("trsum");
  if(!d||d.error){ el.innerHTML=`<div style="color:var(--faint)">${(d&&d.error)||"unavailable"}</div>`; sum.textContent=""; return; }
  let html=`<div class="obhead"><span>Price</span><span>Amount</span><span>Time</span></div>`;
  html+=(d.trades||[]).map(t=>`<div class="obln">`
    +`<span class="ob-${t.side==='sell'?'ask':'bid'}">${price(t.price)}</span>`
    +`<span>${t.amount.toFixed(4)}</span><span style="color:var(--faint)">${t.time}</span></div>`).join("");
  el.innerHTML=html;
  sum.textContent=d.source?`${d.pair} · ${d.source}`:"";
}
// order book depth curve (cumulative bid/ask staircase, like ApeX's "Depth" tab)
function drawDepth(d){
  const cv=$("depthchart"); if(!cv) return;
  const ctx=cv.getContext("2d");
  if(!d||d.error||!(d.bids&&d.bids.length)||!(d.asks&&d.asks.length)){
    ctx.setTransform(1,0,0,1,0,0); ctx.clearRect(0,0,cv.width,cv.height);
    $("obsum").textContent=(d&&d.error)||""; return;
  }
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth||600, h=cv.clientHeight||300;
  cv.width=w*dpr; cv.height=h*dpr; ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
  const bids=d.bids, asks=d.asks;
  const bestBid=bids[0][0], bestAsk=asks[0][0], mid=(bestBid+bestAsk)/2;
  const minP=bids[bids.length-1][0], maxP=asks[asks.length-1][0];
  const maxCum=Math.max(bids[bids.length-1][1], asks[asks.length-1][1])||1;
  const padL=8,padR=8,padT=12,padB=22, plotW=w-padL-padR, plotH=h-padT-padB, baseY=padT+plotH;
  const X=p=>padL+((p-minP)/((maxP-minP)||1))*plotW;
  const Y=c=>padT+plotH-(c/maxCum)*plotH;
  const teal=css("--teal"), brick=css("--brick");
  const side=(pts,col)=>{
    ctx.beginPath(); ctx.moveTo(X(pts[0][0]),baseY);
    pts.forEach(([p,c])=>ctx.lineTo(X(p),Y(c)));
    ctx.lineTo(X(pts[pts.length-1][0]),baseY); ctx.closePath();
    ctx.fillStyle=col; ctx.globalAlpha=0.14; ctx.fill(); ctx.globalAlpha=1;
    ctx.beginPath(); pts.forEach(([p,c],i)=>{const x=X(p),y=Y(c); i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.strokeStyle=col; ctx.lineWidth=1.6; ctx.stroke();
  };
  side(bids,teal); side(asks,brick);
  ctx.strokeStyle=css("--faint"); ctx.setLineDash([4,4]); ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(X(mid),padT); ctx.lineTo(X(mid),baseY); ctx.stroke(); ctx.setLineDash([]);
  ctx.fillStyle=css("--muted"); ctx.font="10px ui-monospace,monospace"; ctx.textBaseline="top";
  ctx.textAlign="left";   ctx.fillText(price(minP), padL, baseY+6);
  ctx.textAlign="center"; ctx.fillText(price(mid),  X(mid), baseY+6);
  ctx.textAlign="right";  ctx.fillText(price(maxP), w-padR, baseY+6);
  $("obsum").textContent=d.source?`${d.pair} · ${d.source}`:"";
}
let _obMode="ladder";
function setObMode(m){
  _obMode=m; const lad=(m==="ladder");
  $("orderbook").style.display=lad?"block":"none";
  $("depthcard").style.display=lad?"none":"block";
  $("obLadder").setAttribute("aria-pressed",lad);
  $("obDepth").setAttribute("aria-pressed",!lad);
  pollMarket();
}
$("obLadder").onclick=()=>setObMode("ladder");
$("obDepth").onclick=()=>setObMode("depth");

let _mktBusy=false;
async function pollMarket(){
  if(_mktBusy||document.hidden) return;
  const a=encodeURIComponent(($("chartAsset")&&$("chartAsset").value)||"BTC");
  _mktBusy=true;
  try{
    const tasks=[ fetch("/api/trades?asset="+a,{cache:"no-store"}).then(r=>r.json()).then(renderTrades) ];
    if(_obMode==="depth")
      tasks.push(fetch("/api/depth?asset="+a,{cache:"no-store"}).then(r=>r.json()).then(drawDepth));
    else
      tasks.push(fetch("/api/orderbook?asset="+a,{cache:"no-store"}).then(r=>r.json()).then(renderOrderBook));
    await Promise.all(tasks);
  }catch(e){}
  _mktBusy=false;
}
setInterval(pollMarket,5000); pollMarket();

// ---- theme toggle (dark / light) ----
function applyChartTheme(){
  if(!_chart) return;
  _chart.applyOptions({
    layout:{background:{color:css("--surface")},textColor:css("--muted")},
    grid:{vertLines:{color:css("--line")},horzLines:{color:css("--line")}},
    timeScale:{borderColor:css("--line")},rightPriceScale:{borderColor:css("--line")}});
}
function setTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  localStorage.setItem("deskTheme",t);
  $("themeToggle").textContent = t==="light" ? "🌙 Dark mode" : "☀️ Light mode";
  applyChartTheme();
  // rebuild the TradingView widget so it matches the theme (only once it's loaded)
  if(typeof _tvView!=="undefined" && _tvView==="trader" && window.TradingView)
    buildTV(tvSymbol($("chartAsset").value||"BTC"));
}
$("themeToggle").onclick=()=>{
  const cur=document.documentElement.getAttribute("data-theme")==="light"?"light":"dark";
  setTheme(cur==="light"?"dark":"light");
};
setTheme(localStorage.getItem("deskTheme")||"dark");
switchChartView("trader");   // default to the ApeX-style Trader view (theme now set)
const money=(v,cur="USD")=>{const s=cur==="INR"?"₹":"$";return s+(v>=1000?v.toLocaleString(undefined,{maximumFractionDigits:0}):v.toFixed(2));};
const pct=v=>(v>=0?"+":"")+v.toFixed(2)+"%";
const cls=v=>v>0?"pos":v<0?"neg":"";
// drawdown is stored as a positive number but rendered as a negative % (red)
const dd=v=>v>0.001?("-"+v.toFixed(2)+"%"):v<-0.001?("+"+(-v).toFixed(2)+"%"):"0.00%";
const clsdd=v=>v>0.001?"neg":"";
const price=v=>v>=1000?"$"+v.toLocaleString(undefined,{maximumFractionDigits:0}):v>=1?"$"+v.toFixed(2):"$"+v.toFixed(3);

let _sparkN=0;
function spark(vals){
  if(!vals||vals.length<2) return `<div class="sparkempty">no history yet — builds as trades close</div>`;
  const w=100,h=40,pad=3, lo=Math.min(...vals), hi=Math.max(...vals), rng=(hi-lo)||1;
  const X=i=>(i/(vals.length-1))*w;
  const Y=v=>pad+(h-2*pad)-((v-lo)/rng)*(h-2*pad);
  const pts=vals.map((v,i)=>X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" ");
  const up=vals[vals.length-1]>=vals[0], col=up?"var(--teal)":"var(--brick)";
  const area=`0,${h} `+pts+` ${w},${h}`;
  const id="sg"+(_sparkN++), yl=Y(vals[vals.length-1]).toFixed(1);
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${col}" stop-opacity="0.30"/>
      <stop offset="1" stop-color="${col}" stop-opacity="0.02"/></linearGradient></defs>
    <line x1="0" y1="${yl}" x2="${w}" y2="${yl}" stroke="${col}" stroke-width="1" stroke-dasharray="2 3"
      opacity="0.35" vector-effect="non-scaling-stroke"/>
    <polygon points="${area}" fill="url(#${id})"/>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.9" vector-effect="non-scaling-stroke"
      stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

/* ---- BOT TABLE ----
   Design brief: the Bots block is the glance surface of the desk — "is anything
   red / dead / not trading?" answered without scrolling. So the default row
   carries only the decision-grade fields (status, name, 30d shape, wallet, P&L,
   trades, win rate) and everything that used to bloat the tile (open positions,
   watchlist, full-size equity chart) moves behind a click. Fixes: 11 bots no
   longer overflow the viewport, and the numbers line up column-wise so bots can
   be compared against each other instead of read one card at a time. */
const BOT_GROUPS=[
  ["Crypto",     ["Futures","ETH Futures","Braked Hold"]],
  ["Short-term", ["Scalp"]],
  ["Paper equity", ["S&P 500","Nifty 50","ONGC","ITC","BTC"]],
];
const botOpen=new Set();   // expanded rows, kept across the live re-render
let _lastBots=[];          // so a toggle can repaint without waiting for /api/overview
let _botsWired=false;

// 60x18 row sparkline — shape only (is it grinding up or bleeding?); the full
// 30-day chart with the last-value guide line still lives in the expand.
function sparkmini(vals){
  if(!vals||vals.length<2) return `<span class="nospark">—</span>`;
  const w=60,h=18,pad=2, lo=Math.min(...vals), hi=Math.max(...vals), rng=(hi-lo)||1;
  const pts=vals.map((v,i)=>((i/(vals.length-1))*w).toFixed(1)+","
    +(pad+(h-2*pad)-((v-lo)/rng)*(h-2*pad)).toFixed(1)).join(" ");
  const col=vals[vals.length-1]>=vals[0]?"var(--teal)":"var(--brick)";
  return `<svg class="minispark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
    +`<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.4"`
    +` vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
}

function botDetail(b){
  const opens=b.open.length?b.open.map(o=>
    `<div class="pos-row"><span><i class="dot" style="background:${o.profit>=0?'var(--teal)':'var(--brick)'}"></i>${o.pair} <span class="tag ${o.dir.toLowerCase()}">${o.dir}</span></span>`
    +`<span class="${cls(o.profit)}">${pct(o.profit)} <span class="p">·${money(o.stake)}</span></span></div>`).join("")
    :`<div class="none">no open positions</div>`;
  const openBases=new Set(b.open.map(o=>o.pair.split('/')[0]));
  const watch=(b.watching&&b.watching.length)?
    `<div class="watching">`
    +b.watching.map(c=>`<span class="wc${openBases.has(c)?' on':''}">${c}${openBases.has(c)?' ●':''}</span>`).join('<span class="wsep">·</span>')
    +`</div>`:`<div class="none" style="font-family:var(--mono);font-size:12px;color:var(--faint)">no whitelist reported</div>`;
  return `<div class="botdet">
      <div><span class="label">Open positions ${b.open.length?"("+b.open.length+")":""}</span>
        <div class="opens">${opens}</div></div>
      <div><span class="label">Watching ${b.watching?b.watching.length:0}</span>${watch}
        <span class="label" style="margin-top:12px">30-day equity</span>${spark(b.equity)}</div>
    </div>`;
}

function botRow(b,cat){
  const sc=!b.online?"off":b.state==="running"?"run":"paused";
  const stxt=!b.online?"offline":b.state;
  const health=!b.online?"flat":b.profit_pct>0.001?"up":b.profit_pct<-0.001?"down":"flat";
  const parrow=b.profit_pct>0.001?"▲ ":b.profit_pct<-0.001?"▼ ":"";
  const wr=b.trades?Math.round(b.winrate):0;
  const ex=botOpen.has(b.name);
  const bal=b.has_balance?money(b.balance,b.currency||"USD"):"—";
  // SQUARE TILE: basic info always visible; advanced (open positions + watchlist +
  // 30d equity) shows when expanded via the existing botOpen toggle. Category tag
  // replaces the old CRYPTO / SHORT-TERM / PAPER EQUITY section headers.
  return `<div class="tile ${health}${b.online?" on":" "}${ex?" detwrap":""}" role="button" tabindex="0"
      aria-expanded="${ex}" data-bot="${b.name}" title="${b.name} — click for open positions &amp; watchlist">
      <div class="thead"><span class="grip" title="Drag to reorder" aria-hidden="true">⋮⋮</span><i class="sdot"></i><span class="tname">${b.name}</span><span class="tcat">${cat}</span></div>
      <div class="tdesc">${b.desc}</div>
      <div class="tmetrics">
        <div class="tm"><span class="k">Wallet</span><span class="v">${bal}</span></div>
        <div class="tm"><span class="k">Closed P&amp;L</span><span class="v ${cls(b.profit_pct)}">${parrow}${pct(b.profit_pct)}</span></div>
        <div class="tm"><span class="k">Trades</span><span class="v sm">${b.trades}</span></div>
        <div class="tm"><span class="k">Win</span><span class="v sm">${b.trades?wr+"%":"—"}</span></div>
        <div class="tm full"><span class="k">Max DD</span><span class="v ${clsdd(b.max_dd)}">${dd(b.max_dd)}</span></div>
      </div>
      <div class="tspark">${sparkmini(b.equity)}</div>
      <div class="tfoot"><span class="state ${sc}"><i class="livedot"></i>${stxt}</span>${b.open.length?` <span class="opct">${b.open.length} open</span>`:""}<span class="tchev">›</span></div>
      ${ex?botDetail(b):""}
    </div>`;
}

function renderBots(bots){
  _lastBots=bots;
  // flatten all groups into one tile grid; tag each tile with its category instead
  // of splitting into CRYPTO / SHORT-TERM / PAPER EQUITY section headers.
  const catOf={};
  BOT_GROUPS.forEach(([title,names])=>names.forEach(n=>catOf[n]=title));
  bots.forEach(b=>{ if(!catOf[b.name]) catOf[b.name]="Other"; });

  // apply persisted manual ordering (drag-reorder) on top of the incoming list
  let ordered=bots.slice();
  try{
    const saved=JSON.parse(localStorage.getItem("botsOrder")||"null");
    if(Array.isArray(saved) && saved.length){
      const byName=new Map(bots.map(b=>[b.name,b]));
      const seen=new Set();
      const out=[];
      saved.forEach(n=>{ if(byName.has(n) && !seen.has(n)){ out.push(byName.get(n)); seen.add(n); } });
      bots.forEach(b=>{ if(!seen.has(b.name)) out.push(b); });   // append any new bots
      ordered=out;
    }
  }catch(e){ /* bad stored order: ignore */ }

  const bal=ordered.reduce((s,b)=>s+(b.has_balance?b.balance:0),0);
  const live=ordered.filter(b=>b.online).length;

  // Flat layout: no section holder — each tile is a direct child of #bots so it
  // expands independently (CSS grid align-items:start keeps row-mates from stretching).
  $("bots").innerHTML=`<div class="botgrphd"><span class="label">BOTS</span>`+
    `<span class="gsum">${live}/${ordered.length} live · ${money(bal)}</span></div>`+
    `<div class="tilegrid">${ordered.map(b=>botRow(b,catOf[b.name])).join("")}</div>`;

  if(!_botsWired){          // delegated once — innerHTML is replaced on every tick
    const host=$("bots");
    const toggle=el=>{const n=el.dataset.bot; botOpen.has(n)?botOpen.delete(n):botOpen.add(n); renderBots(_lastBots);};
    host.addEventListener("click",e=>{
      const r=e.target.closest(".tile"); if(!r) return;
      if(e.target.closest(".grip")) return;        // grip is for dragging, not toggling
      toggle(r);
    });
    host.addEventListener("keydown",e=>{
      if(e.key!=="Enter"&&e.key!==" ") return;
      const r=e.target.closest(".tile"); if(!r) return;
      e.preventDefault(); toggle(r);
    });
    initDrag(host);
    _botsWired=true;
  }
}

// ---- drag-to-reorder with FLIP animation (smooth, no libs) ----
// Approach: on pointerdown the dragged tile is lifted out of grid flow (position:fixed)
// and a same-size placeholder takes its slot. During the move we only shuffle the PLACEHOLDER
// between siblings; siblings FLIP (animate) to their new slots while the dragged tile follows
// the cursor. Because the dragged tile is never re-inserted, it cannot jump -> no flicker.
let _drag=null;
function initDrag(host){
  host.addEventListener("pointerdown",e=>{
    const grip=e.target.closest(".grip"); if(!grip) return;
    const tile=grip.closest(".tile"); if(!tile) return;
    e.preventDefault();
    const grid=host.querySelector(".tilegrid");

    // snapshot the tile's position/size, then lift it out of flow
    const rect=tile.getBoundingClientRect();
    const ph=document.createElement("div");
    ph.className="tile-placeholder";
    ph.style.width=rect.width+"px"; ph.style.height=rect.height+"px";
    tile.parentNode.insertBefore(ph, tile);

    tile.classList.add("dragging");
    tile.style.width=rect.width+"px";
    tile.style.height=rect.height+"px";
    tile.style.left=rect.left+"px";
    tile.style.top=rect.top+"px";
    grid.classList.add("dragging-on");

    _drag={tile,grid,ph,offX:e.clientX-rect.left,offY:e.clientY-rect.top,
           startX:e.clientX,startY:e.clientY,moved:false,lastOver:ph};

    tile.setPointerCapture(e.pointerId);
    host.addEventListener("pointermove",onDragMove);
    host.addEventListener("pointerup",onDragEnd);
    host.addEventListener("pointercancel",onDragEnd);
  });
}
function onDragMove(e){
  if(!_drag) return;
  const {tile,grid,ph,offX,offY}=_drag;
  _drag.moved=true;
  // the tile simply follows the cursor (fixed positioning, no layout anchor to jump)
  tile.style.left=(e.clientX-offX)+"px";
  tile.style.top =(e.clientY-offY)+"px";

  // figure out which real tile we're hovering (ignore the fixed tile + placeholder)
  tile.style.pointerEvents="none";
  const el=document.elementFromPoint(e.clientX,e.clientY);
  tile.style.pointerEvents="";
  const over=el&&el.closest&&el.closest(".tile");
  if(over && over!==tile && over!==ph){
    const r=over.getBoundingClientRect();
    const after=(e.clientY-r.top)>r.height/2 || (e.clientX-r.left)>r.width/2;
    const target=after?over.nextSibling:over;
    if(ph!==target){
      // FLIP: record current sibling positions, move placeholder, then animate siblings
      const sibs=[...grid.querySelectorAll(".tile")].filter(t=>t!==tile);
      const first=new Map(sibs.map(t=>[t,t.getBoundingClientRect()]));
      if(after) over.after(ph); else over.before(ph);
      sibs.forEach(t=>{
        const f=first.get(t), l=t.getBoundingClientRect();
        const ddx=f.left-l.left, ddy=f.top-l.top;
        if(ddx||ddy){
          t.style.transition="none";
          t.style.transform=`translate(${ddx}px,${ddy}px)`;
          requestAnimationFrame(()=>{
            t.style.transition="transform .18s cubic-bezier(.2,.7,.3,1)";
            t.style.transform="";
          });
        }
      });
      _drag.lastOver=over;
    }
  }
}
function onDragEnd(e){
  if(!_drag) return;
  const {tile,grid,ph,moved}=_drag;
  const host=grid.closest("#bots")||document;
  host.removeEventListener("pointermove",onDragMove);
  host.removeEventListener("pointerup",onDragEnd);
  host.removeEventListener("pointercancel",onDragEnd);

  // settle the dragged tile into the placeholder's slot (smooth) then normalize
  const pr=ph.getBoundingClientRect();
  const tr=tile.getBoundingClientRect();
  tile.style.transition="none";
  tile.style.transform=`translate(${pr.left-tr.left}px,${pr.top-tr.top}px)`;
  requestAnimationFrame(()=>{
    tile.style.transition="left .18s cubic-bezier(.2,.7,.3,1),top .18s cubic-bezier(.2,.7,.3,1)";
    tile.style.left=pr.left+"px"; tile.style.top=pr.top+"px"; tile.style.transform="";
  });
  const finish=()=>{
    if(finish.done) return; finish.done=true;
    tile.classList.remove("dragging");
    tile.style.cssText="";               // drop fixed/width/height/left/top
    if(ph.parentNode) ph.parentNode.replaceChild(tile, ph);
    grid.classList.remove("dragging-on");
    [...grid.querySelectorAll(".tile")].forEach(t=>{ t.style.transition=""; t.style.transform=""; t.style.willChange=""; });
    if(moved) saveOrder(grid);
  };
  tile.addEventListener("transitionend", finish, {once:true});
  setTimeout(finish, 260);               // fallback if transitionend doesn't fire
  _drag=null;
}
function saveOrder(grid){
  const order=[...grid.querySelectorAll(".tile")].map(t=>t.dataset.bot);
  try{ localStorage.setItem("botsOrder",JSON.stringify(order)); }catch(e){}
}

function drawEquityChart(hist){
  const cv=document.getElementById("eqchart"), empty=document.getElementById("eqempty");
  if(!hist || hist.length<2){
    cv.style.display="none"; empty.style.display="block";
    empty.textContent=`Equity history builds over time — needs a few days to plot (${hist?hist.length:0} point${hist&&hist.length===1?"":"s"} so far).`;
    return;
  }
  cv.style.display="block"; empty.style.display="none";
  const dpr=window.devicePixelRatio||1, w=cv.clientWidth, h=260;
  cv.width=w*dpr; cv.height=h*dpr; const ctx=cv.getContext("2d"); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  const series=[
    {k:"brakedhold",color:css("--amber"),lw:2.4,dash:[]},
    {k:"spot",color:"#6BA5E0",lw:1.5,dash:[]},
    {k:"futures",color:"#A98BE0",lw:1.5,dash:[]},
    // apex series removed 2026-07-23 (bot retired; see BOTS list)
    {k:"spx",color:"#4ADE80",lw:1.5,dash:[]},
    {k:"nifty",color:"#FF7AB6",lw:1.5,dash:[]},
    {k:"ongc",color:"#FFB347",lw:1.5,dash:[]},
    {k:"itc",color:"#9ACD32",lw:1.5,dash:[]},
    {k:"btc",color:"#F7931A",lw:1.5,dash:[]},
    {k:"btc_hold",color:css("--brick"),lw:1.5,dash:[5,4]},
    {k:"basket_hold",color:css("--teal"),lw:1.5,dash:[5,4]},
  ];
  const padL=52,padR=12,padT=12,padB=22,plotW=w-padL-padR,plotH=h-padT-padB;
  let lo=Infinity,hi=-Infinity;
  hist.forEach(r=>series.forEach(s=>{const v=r[s.k]; if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v);}}));
  if(!isFinite(lo))return;
  const pad=(hi-lo)*0.10||10; lo-=pad; hi+=pad;
  const X=i=>padL+(i/(hist.length-1))*plotW;
  const Y=v=>padT+plotH-((v-lo)/(hi-lo))*plotH;
  ctx.font="10px "+css("--mono"); ctx.lineWidth=1;
  for(let g=0;g<=4;g++){const v=lo+(hi-lo)*g/4,y=Y(v);
    ctx.strokeStyle=css("--line"); ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(w-padR,y); ctx.stroke();
    ctx.fillStyle=css("--muted"); ctx.textAlign="right"; ctx.textBaseline="middle"; ctx.fillText("$"+v.toFixed(0),padL-6,y);}
  ctx.textAlign="center"; ctx.textBaseline="top";
  [...new Set([0,Math.floor(hist.length/2),hist.length-1])].forEach(i=>ctx.fillText(hist[i].date.slice(5),X(i),h-padB+5));
  series.forEach(s=>{
    ctx.strokeStyle=s.color; ctx.lineWidth=s.lw; ctx.setLineDash(s.dash); ctx.lineJoin="round";
    ctx.beginPath(); let started=false;
    hist.forEach((r,i)=>{const v=r[s.k]; if(v==null)return; const x=X(i),y=Y(v); started?ctx.lineTo(x,y):ctx.moveTo(x,y); started=true;});
    ctx.stroke();
  });
  ctx.setLineDash([]);
}

// ---- backtest lab: load once on page load (static data, not polled) ----
function fmtPct(v){return v==null?"—":(v>=0?"+":"")+v.toFixed(1)+"%";}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function fmtNum(v,d=2){return v==null?"—":v.toFixed(d);}
function fmtInf(v,d=2){return v==null?"∞":v.toFixed(d);}
function fmtInt(v){return v==null?"—":String(v);}
function btClass(v){return v==null?"inf":v>=0?"pos":"neg";}

async function loadBacktests(){
  try{
    const r=await fetch("/api/backtests",{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const panels=await r.json();
    if(!panels.length){
      $("backtests").innerHTML=`<div class="none mono" style="color:var(--faint);padding:12px 4px">no backtest result files found</div>`;
      return;
    }
    $("backtests").innerHTML=panels.map(p=>{
      // meta line: date range, bars, pair, fee, tax rate — whatever the file exposes
      const m=p.meta||{};
      const metaBits=[];
      if(m.pair) metaBits.push(m.pair);
      if(m.bars) metaBits.push(m.bars+" bars");
      if(m.date_range && Array.isArray(m.date_range)) metaBits.push(m.date_range[0]+" → "+m.date_range[1]);
      if(m.years) metaBits.push(m.years+"y");
      if(m.fee_side!=null) metaBits.push("fee "+(m.fee_side*100).toFixed(2)+"%");
      if(m.tds!=null) metaBits.push("TDS "+(m.tds*100).toFixed(0)+"%");
      if(m.tax_rate!=null) metaBits.push("tax "+(m.tax_rate*100).toFixed(0)+"%");
      const metaHtml=metaBits.length?`<div class="btmeta">${metaBits.map(b=>`<span>${b}</span>`).join("")}</div>`:"";

      const rows=p.results.map(r=>{
        // highlight benchmark rows (B&H, DCA) with muted styling
        const isBench=/^(B&H|DCA|Buy & Hold|Benchmark)/i.test(r.variant||"");
        const vc=isBench?"bench":"";
        const cagrC=btClass(r.cagr);
        const ddC=r.max_dd!=null&&r.max_dd<0?"neg":"inf";
        const pfC=r.pf==null?"inf":r.pf>=1?"pos":"neg";
        return `<tr>
          <td class="${vc}" title="${(r.variant||"").replace(/"/g,"&quot;")}">${r.variant||"—"}</td>
          <td class="${cagrC}">${fmtPct(r.cagr)}</td>
          <td class="${ddC}">${fmtPct(r.max_dd)}</td>
          <td>${fmtInt(r.trades)}</td>
          <td>${r.win_rate!=null?Math.round(r.win_rate)+"%":"—"}</td>
          <td class="${pfC}">${fmtInf(r.pf)}</td>
        </tr>`;
      }).join("");

      return `<div class="btcard">
        <div class="bthead"><h3>${p.title}</h3></div>
        ${metaHtml}
        <div class="bttable-wrap">
          <table class="bttable">
            <thead><tr>
              <th>Variant</th><th>CAGR</th><th>Max DD</th><th>Trades</th><th>Win</th><th>PF</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
    }).join("");
  }catch(e){
    $("backtests").innerHTML=`<div class="none mono" style="color:var(--faint);padding:12px 4px">couldn't load backtests: ${e.message}</div>`;
  }
}
loadBacktests();

// ---- backtest analyst reports ----
async function loadAnalyses(){
  try{
    const r=await fetch("/api/backtest_analyses",{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const reports=await r.json();
    if(!reports.length){
      $("analyses").innerHTML=`<div class="none mono" style="color:var(--faint);padding:12px 4px">no analyst reports yet — run backtest_analyst.py to generate one</div>`;
      return;
    }
    $("analyses").innerHTML=reports.map(rep=>{
      const ts=rep.timestamp?new Date(rep.timestamp).toLocaleString("en-US",{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}):"—";
      const riskClass=rep.overfitting_risk==="high"?"neg":rep.overfitting_risk==="low"?"pos":"";
      const flags=rep.red_flags||[];
      const cands=rep.improvement_candidates||[];
      const flagHtml=flags.length?`<div class="btmeta" style="margin-top:8px"><span style="color:var(--neg)">🚩 ${flags.length} red flags</span></div>
        <ul class="analyst-list">${flags.map(f=>`<li>${esc(f)}</li>`).join("")}</ul>`:"";
      const candHtml=cands.length?`<div class="btmeta" style="margin-top:8px"><span style="color:var(--pos)">💡 ${cands.length} improvement candidates</span></div>
        <ul class="analyst-list">${cands.map(c=>{
          const ch=typeof c==="object"?esc(c.change||"?"):"—";
          const ra=typeof c==="object"?esc(c.rationale||""):"";
          return `<li><b>${ch}</b>${ra?" — "+ra:""}</li>`;
        }).join("")}</ul>`:"";
      const ddHtml=rep.drawdown_analysis?`<div class="analyst-section"><b>Drawdown:</b> ${esc(rep.drawdown_analysis)}</div>`:"";
      const robHtml=rep.robustness?`<div class="analyst-section"><b>Robustness:</b> ${esc(rep.robustness)}</div>`:"";
      const bmHtml=rep.benchmark_comparison?`<div class="analyst-section"><b>Benchmark:</b> ${esc(rep.benchmark_comparison)}</div>`:"";
      return `<div class="btcard">
        <div class="bthead"><h3>${ts}</h3>
          <span class="label" style="color:var(--faint)">${rep.confidence||"?"} confidence</span></div>
        <div class="btmeta">
          <span class="${riskClass}">overfit: ${esc(rep.overfitting_risk||"?")}</span>
          <span>${(rep.files_analyzed||[]).length} files</span>
        </div>
        <div class="analyst-summary">${esc(rep.summary||"—")}</div>
        ${ddHtml}${robHtml}${bmHtml}
        ${flagHtml}${candHtml}
      </div>`;
    }).join("");
  }catch(e){
    $("analyses").innerHTML=`<div class="none mono" style="color:var(--faint);padding:12px 4px">couldn't load analyst reports: ${e.message}</div>`;
  }
}
loadAnalyses();

let tickBusy=false;             // in-flight guard: a hung bot must not stack requests
let niftyBusy=false;

const _niftyInr=v=>"₹"+v.toLocaleString(undefined,{maximumFractionDigits:0});
const _niftyPct=v=>(v>=0?"+":"")+v.toFixed(2)+"%";

async function loadNiftyPaperDesk(){
  if(niftyBusy) return;
  niftyBusy=true;
  try{
    const r=await fetch("/nifty/api/stats",{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d=await r.json();
    const s=d.stats||{};
    const logTail=(d.log||[]).slice(-3).join(" · ")||"no log";
    let html=`<div class="fund" style="gap:18px 34px">`
      +`<div><span class="label">Trades</span><b style="font-size:20px">${s.trades}</b></div>`
      +`<div><span class="label">Win rate</span><b style="font-size:20px">${s.win_rate_pct}%</b></div>`
      +`<div><span class="label">Profit factor</span><b style="font-size:20px">${s.profit_factor}</b></div>`
      +`<div><span class="label">Total P&amp;L</span><b class="${s.total_pnl>=0?'pos':'neg'}" style="font-size:20px">${_niftyInr(s.total_pnl)}</b></div>`
      +`<div><span class="label">Total return</span><b class="${s.total_return_pct>=0?'pos':'neg'}" style="font-size:20px">${_niftyPct(s.total_return_pct)}</b></div>`
      +`<div><span class="label">Max DD</span><b class="neg" style="font-size:20px">${s.max_drawdown_pct}%</b></div>`
      +`</div>`;
    if(s.equity && s.equity.length>1) html+=`<div style="margin-top:10px">${spark(s.equity)}</div>`;
    if(d.open && d.open.length){
      html+=`<div class="label" style="margin-top:14px">Open position</div>`
        +`<div class="mono" style="font-size:12px">`
        +d.open.map(o=>`${o.side.toUpperCase()} ${o.opt_type} ${o.strike} @ ${o.entry_time} · lots ${o.lots}`).join(", ")
        +`</div>`;
    }
    html+=`<div style="margin-top:8px;color:var(--faint);font-size:11px" class="mono">latest log: ${logTail}</div>`;
    $("niftyDesk").innerHTML=html;
    $("niftyLast").textContent=`paper · ${s.trades} closed · latest: ${logTail}`;
  }catch(e){
    $("niftyDesk").innerHTML=`<div style="color:var(--brick);font-size:12px">Nifty 5m paper view temporarily unavailable</div>`;
  }finally{
    niftyBusy=false;
  }
}

async function tick(){
  if(tickBusy) return;
  tickBusy=true;
  try{
    const r=await fetch("/api/overview",{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d=await r.json();
    $("err").hidden=true;
    const miss=d.bots_missing||[];
    $("tbal").textContent=money(d.total_balance)+(miss.length?` (${d.bots_reporting} of ${d.bots.length} bots)`:"");
    $("tpnl").textContent=miss.length?pct(d.total_pnl_pct)+" · partial — "+miss.join(", ")+" offline":pct(d.total_pnl_pct);
    $("tpnl").className="big "+cls(d.total_pnl_pct);
    // equal-weighted sub-line + INR rate note
    const eq=d.equal_weighted_pct;
    const fx=d.usdinr_rate;
    $("tpnl_sub").textContent=`Equal-weighted: ${pct(eq)}  ·  INR bots converted at ₹${fx.toFixed(2)}/USD`;
    $("tpnl_sub").className="mono "+cls(eq);
    $("upd").textContent="live · "+new Date().toLocaleTimeString();

    // guardian
    const g=$("guard"); g.hidden=false;
    g.className="guard"+(d.guardian.tripped?" tripped":"");
    const gcol=d.guardian.tripped?"var(--brick)":d.guardian.paused?"var(--amber)":"var(--teal)";
    const gtxt=d.guardian.tripped?"CIRCUIT BREAKER TRIPPED — halted":d.guardian.paused?"paused by guardian":"armed · watching";
    g.innerHTML=`<span class="chip"><i class="s" style="background:${gcol}"></i>🛡️ Guardian: <b>${gtxt}</b></span>`
      +`<span style="color:var(--muted)">peak equity ${money(d.guardian.peak)}</span>`;

    // bots
    renderBots(d.bots);

    // brake board
    $("brakesum").textContent=d.brake_total?`${d.brake_hold} holding · ${d.brake_total-d.brake_hold} in cash`:"";
    $("board").innerHTML=d.brake.length?d.brake.map(c=>{
      const hold=c.state==="above";
      return `<div class="coin ${hold?"hold":"cash"}">
        <div class="c"><i class="s" style="background:${hold?"var(--teal)":"var(--brick)"}"></i>${c.coin}</div>
        <div class="g ${hold?"pos":"neg"}">${hold?"HOLD":"CASH"} ${(c.gap>=0?"+":"")+c.gap.toFixed(0)}%</div>
        <div class="g" style="color:var(--faint)">${price(c.price)}</div></div>`;
    }).join(""):`<div class="none mono" style="color:var(--faint)">brake state not yet cached — the hourly job populates it</div>`;

    // shared "frozen" badge: these panels read CACHED board files, so when the
    // generating job is paused (2026-07-20 simplification) the panel is truly stale.
    const fbadge=(iso)=>{
      let w="";
      if(iso){w=" · last updated "+String(iso).slice(0,16).replace("T"," ")+" UTC";}
      return `<div class="mono" style="font-size:11px;color:var(--amber);`
        +`border:1px solid rgba(240,168,60,.35);border-radius:6px;padding:4px 8px;margin-bottom:8px">`
        +`⏸ FROZEN — auto-monitor paused${w}. For a live reading use Telegram /trend · /portfolio</div>`;};

    // diversified braked portfolio map — India (tradeable) + US (reference)
    const pf=d.portfolio||{};
    const row=s=>{
      let dot,txt;
      if(s.kind==="crypto"){const f=s.frac||0;
        dot=f>=0.5?"var(--teal)":(f>0?"var(--amber)":"var(--brick)");
        txt=`${Math.round(f*100)}% in · ${s.above}/${s.total} above line`;}
      else{const up=s.state==="above";
        dot=up?"var(--teal)":"var(--brick)";
        txt=`${up?"HOLD":"CASH"} · ${(s.symbol||"").replace(".NS","")} ${(s.gap>=0?"+":"")+Math.round(s.gap)}%`;}
      return `<div class="pfrow"><i class="s" style="background:${dot}"></i>
        <span class="nm">${s.name}</span><span class="st">${txt}</span></div>`;};
    const block=(flag,label,v,primary)=>{
      const dep=Math.round((v.deployed||0)*100);
      return `<div class="pfhead" style="${primary?'':'margin-top:12px;'}">`
        +`<span style="color:var(--muted)">${flag} ${label}</span>`
        +`<span class="dep" style="${primary?'':'font-size:15px'}">${dep}% <span style="font-size:11px;color:var(--faint)">deployed</span></span></div>`
        +`<div class="pfbar"><i style="width:${dep}%"></i></div>`
        +v.sleeves.map(row).join("");};
    if(pf.india||pf.us){
      const di=pf.india?Math.round(pf.india.deployed*100):null;
      $("pfsum").textContent=di!==null?`India ${di}% deployed · ${100-di}% cash`:"";
      let html=pf._paused?fbadge(pf.ts):"";
      if(pf.india) html+=block("🇮🇳","INDIA · Zerodha-tradeable",pf.india,true);
      if(pf.us) html+=block("🇺🇸","US · reference (LRS)",pf.us,false);
      html+=`<div class="pfrow" style="color:var(--faint);font-size:11px;border-top:1px solid var(--line);padding-top:8px">`
        +`each sleeve ≈25% · idle cash earns yield · India ≈⅓ crypto's drawdown (backtested)</div>`;
      $("portfolio").innerHTML=html;
    } else {
      $("pfsum").textContent="";
      $("portfolio").innerHTML=`<div class="none mono" style="color:var(--faint);padding:8px 4px">portfolio map not yet cached — the 6h job populates it (or send /portfolio)</div>`;
    }

    // activity feed — the same push-alerts sent to Telegram
    const relt=iso=>{const s=(Date.now()-new Date(iso).getTime())/1000;
      if(s<60)return"just now";if(s<3600)return Math.floor(s/60)+"m ago";
      if(s<86400)return Math.floor(s/3600)+"h ago";return Math.floor(s/86400)+"d ago";};
    const badge={trade:"var(--teal)",guardian:"var(--amber)",brake:"var(--blue)",macro:"var(--blue)",
                 portfolio:"var(--teal)",watchdog:"var(--brick)",sharp:"var(--amber)",review:"var(--muted)",report:"var(--muted)"};
    const feed=d.activity||[];
    $("activity").innerHTML=feed.length?feed.map(e=>{
      const first=(e.text||"").split("\\n")[0];
      const col=badge[e.source]||"var(--muted)";
      return `<div class="ev"><span class="src" style="color:${col}">${e.source}</span>
        <div class="evtxt"><div class="t1">${first.replace(/</g,"&lt;")}</div>
        <div class="tm">${relt(e.ts)}</div></div></div>`;
    }).join(""):`<div class="none mono" style="color:var(--faint);padding:12px 14px">no activity yet — alerts appear here as they're sent to Telegram</div>`;

    // equity vs benchmarks chart
    drawEquityChart(d.equity_history); _lastEquity=d.equity_history||null;

    // brake memory
    const m=d.memory;
    if(m){
      let head;
      if(m.completed>0){
        head=`<div class="m"><span class="label">Completed holds</span><span class="v">${m.completed}</span></div>`
          +`<div class="m"><span class="label">Win rate</span><span class="v">${Math.round(m.win_rate)}%</span></div>`
          +`<div class="m"><span class="label">Avg return</span><span class="v ${cls(m.avg_ret)}">${pct(m.avg_ret)}</span></div>`
          +`<div class="m"><span class="label">Avg hold</span><span class="v">${Math.round(m.avg_days)}d</span></div>`
          +`<div class="m"><span class="label">Best / worst</span><span class="v" style="font-size:14px"><span class="pos">${pct(m.best.ret_pct)}</span> / <span class="neg">${pct(m.worst.ret_pct)}</span></span></div>`;
      } else {
        head=`<span class="none">No completed holds yet — the record fills in as coins flip back to cash.</span>`;
      }
      const holds=m.open.length?`<div class="holds"><span class="label">Currently holding (${m.open.length})</span>`
        +m.open.map(o=>`<div class="hrow"><span>${o.coin} <span class="p">· since ${o.since} (~${o.days}d)</span></span>`
          +`<span class="${o.unreal>=0?'pos':'neg'}">${o.unreal!=null?pct(o.unreal)+" unrealized":""}</span></div>`).join("")+`</div>`:"";
      $("memory").innerHTML=`<div class="row1">${head}</div>${holds}`;
    } else {
      $("memory").innerHTML=`<span class="none">memory not available</span>`;
    }

  }catch(e){
    $("err").hidden=false; $("err").textContent="⚠️ can't reach the desk API: "+e.message;
    $("upd").textContent="disconnected"; $("livedot").style.background="var(--brick)";
  }finally{
    tickBusy=false;
    loadNiftyPaperDesk();
  }
}
tick(); setInterval(tick, 15000);

// --- action buttons (read-only: live brake check + full memory) ---
function openModal(title,html){$("modalTitle").innerHTML=title;$("modalBody").innerHTML=html;$("modal").classList.add("open");}
$("modalClose").onclick=()=>$("modal").classList.remove("open");
$("modal").onclick=e=>{if(e.target.id==="modal")$("modal").classList.remove("open");};
document.addEventListener("keydown",e=>{if(e.key==="Escape")$("modal").classList.remove("open");});

$("btnBrake").onclick=async()=>{
  const b=$("btnBrake"),t=b.textContent; b.disabled=true; b.textContent="checking… (~3s)";
  try{
    const d=await (await fetch("/api/brake/live",{cache:"no-store"})).json();
    if(d.error){ openModal("🪂 Brake — live",`<div class="sparkempty">${d.error}</div>`); }
    else{
      const grid=d.coins.map(c=>{
        if(c.error) return `<div class="coin cash"><div class="c">${c.coin}</div><div class="g" style="color:var(--faint)">err</div></div>`;
        const hold=c.state==="above";
        return `<div class="coin ${hold?'hold':'cash'}"><div class="c"><i class="s" style="background:${hold?'var(--teal)':'var(--brick)'}"></i>${c.coin}</div>
          <div class="g ${hold?'pos':'neg'}">${hold?'HOLD':'CASH'} ${(c.gap>=0?'+':'')+c.gap.toFixed(0)}%</div>
          <div class="g" style="color:var(--faint)">${price(c.price)}</div></div>`;
      }).join("");
      openModal("🪂 Brake — live check",
        `<div class="msub">${d.hold}/${d.total} holding · live from ${d.source} · ${new Date().toLocaleTimeString()}</div><div class="mboard">${grid}</div>`);
    }
  }catch(e){ openModal("🪂 Brake — live",`<div class="sparkempty">error: ${e.message}</div>`); }
  b.disabled=false; b.textContent=t;
};

$("btnMem").onclick=async()=>{
  const b=$("btnMem"),t=b.textContent; b.disabled=true; b.textContent="loading…";
  try{
    const d=await (await fetch("/api/memory/full",{cache:"no-store"})).json();
    openModal("🧠 Brake memory — full record",`<pre>${(d.text||"").replace(/[&<]/g,s=>s==="&"?"&amp;":"&lt;")}</pre>`);
  }catch(e){ openModal("🧠 Memory",`<div class="sparkempty">error: ${e.message}</div>`); }
  b.disabled=false; b.textContent=t;
};

// ---- Polymarket copy-trading bot section ----
const escH=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pct2=v=>{const n=Number(v)||0;return (n>=0?"+":"")+n.toFixed(1)+"%";};
let _polyChart=null;

function drawPolyChart(pts){
  const cv=$("polyChart"),ctx=cv.getContext("2d");
  const empty=$("polyChartEmpty");
  if(!pts||pts.length<2){cv.style.display="none";empty.style.display="block";return;}
  cv.style.display="block";empty.style.display="none";
  const dpr=window.devicePixelRatio||1, W=cv.clientWidth||320, H=170;
  cv.width=W*dpr;cv.height=H*dpr;ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,W,H);
  const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const txt=css("--text"),muted=css("--muted"),faint=css("--faint"),line=css("--line"),teal=css("--teal");
  const vals=pts.map(p=>Number(p.eq));
  const min=Math.min(...vals),max=Math.max(...vals),span=(max-min)||1;
  const pad=6;
  const x=i=>pad+(W-2*pad)*i/(pts.length-1);
  const y=v=>H-pad-(H-2*pad)*(v-min)/span;
  // grid
  ctx.strokeStyle=line;ctx.fillStyle=faint;ctx.font="9px ui-monospace,Menlo,monospace";
  for(let g=0;g<=3;g++){const gy=pad+(H-2*pad)*g/3;ctx.beginPath();ctx.moveTo(pad,gy);ctx.lineTo(W-pad,gy);ctx.stroke();
    const gv=max-(max-min)*g/3;ctx.fillText(gv.toFixed(0),2,gy-2);}
  // equity line
  ctx.beginPath();ctx.strokeStyle=teal;ctx.lineWidth=1.6;ctx.lineJoin="round";
  pts.forEach((p,i)=>{const px=x(i),py=y(p.eq);i?ctx.lineTo(px,py):ctx.moveTo(px,py);});
  ctx.stroke();
  // fill
  ctx.lineTo(x(pts.length-1),H-pad);ctx.lineTo(x(0),H-pad);ctx.closePath();
  ctx.fillStyle="rgba(63,199,168,.08)";ctx.fill();
  // last label
  ctx.fillStyle=txt;ctx.font="10px ui-monospace,Menlo,monospace";
  const last=pts[pts.length-1];
  ctx.fillText(`$${Number(last.eq).toFixed(0)}`,W-pad-40,14);
}

async function loadPoly(){
  const grab=async url=>{try{return await (await fetch(url,{cache:"no-store"})).json();}catch(e){return {error:e.message};}};
  const [ov,wallets,positions,rules,tl]=await Promise.all([
    grab("/api/polymarket/overview"),grab("/api/polymarket/wallets"),
    grab("/api/polymarket/positions"),grab("/api/polymarket/rules"),
    grab("/api/polymarket/timeline"),
  ]);
  if(ov.error){$("polylast").textContent="error: "+ov.error;return;}
  $("polylast").textContent=`paper · local · ${ov.total_trades.toLocaleString()} trades ingested`;
  const eq=Number(ov.equity),pnl=Number(ov.pnl);
  $("pEq").textContent="$"+eq.toLocaleString(undefined,{maximumFractionDigits:0});
  $("pEq").style.color=pnl>=0?"var(--teal)":"var(--brick)";
  $("pPnl").textContent=(pnl>=0?"+":"")+pnl.toFixed(2);
  $("pPnl").style.color=pnl>=0?"var(--teal)":"var(--brick)";
  $("pPnlSub").textContent=`dd ${ov.drawdown_pct}% · peak $${Number(ov.peak_equity).toFixed(0)}`;
  $("pWallets").textContent=ov.wallets.toLocaleString();
  $("pWalletsSub").textContent=`${ov.scored_wallets.toLocaleString()} scored · ${ov.total_trades.toLocaleString()} trades`;
  $("pPos").textContent=ov.open_positions;
  $("pPosSub").textContent=`${ov.missed_winners} missed winners · rule v${ov.rule_version}`;

  // decision mix
  const mix=ov.decisions||{};
  const mixEl=$("polyMix");
  if(Object.keys(mix).length){
    mixEl.innerHTML=["COPY","WATCH","SKIP"].map(k=>
      `<span>${k} <b>${mix[k]||0}</b></span>`).join("");
  }else mixEl.innerHTML=`<span class="polyempty" style="display:block;text-align:left;padding:0">no decisions yet</span>`;

  // wallets
  if(Array.isArray(wallets)&&!wallets.error&&wallets.length){
    const rows=wallets.map(w=>{
      const edge=Object.entries(w.category_edge||{})
        .map(([k,v])=>`<span class="polyedge ${Number(v)>=0.55?"hot":""}">${k} ${(Number(v)*100).toFixed(0)}</span>`).join("");
      return `<tr>
        <td style="color:var(--faint)">${w.rank}</td>
        <td><span title="${escH(w.address)}">${escH(w.short)}</span>${w.pseudonym?`<div style="font-size:10px;color:var(--faint)">${escH(w.pseudonym)}</div>`:""}</td>
        <td><div class="polybar"><i style="width:${Math.round(Number(w.composite)*100)}%"></i></div></td>
        <td style="text-align:right">${Number(w.composite).toFixed(2)}</td>
        <td>${edge}</td>
      </tr>`;
    }).join("");
    $("polyWallets").innerHTML=`<thead><tr><th>#</th><th>Wallet</th><th>Score</th><th>Comp</th><th>Category edge</th></tr></thead><tbody>${rows}</tbody>`;
  }

  // positions
  const posEl=$("polyPositions");
  if(positions&&!positions.error&&(positions.open.length||positions.closed.length)){
    const open=positions.open.map(p=>
      `<div class="polypos"><span><span class="mk">${escH(p.side)}</span> ${escH(p.market)} <span class="sd">@ ${p.entry} · $${p.size}</span></span><span class="sd">${escH(String(p.opened).slice(0,16))}</span></div>`).join("");
    const closed=positions.closed.map(p=>
      `<div class="polypos" style="opacity:.7"><span><span class="mk">${escH(p.side)}</span> ${escH(p.market)} <span class="sd">${p.pnl>=0?"+":""}${Number(p.pnl).toFixed(2)} ${p.exit?("· "+escH(p.exit)):""}</span></span><span class="sd">closed</span></div>`).join("");
    posEl.innerHTML=(open?`<div style="font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px">Open (${positions.open.length})</div>${open}`:"")
      +(closed?`<div style="font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em;margin:10px 0 4px">Recently closed</div>${closed}`:"");
  }

  // rules
  const ruleEl=$("polyRules");
  if(rules&&!rules.error&&rules.versions.length){
    ruleEl.innerHTML=rules.versions.map(v=>
      `<div class="polyrule"><b>v${v.id}</b> · ${escH(String(v.from).slice(0,16))} — ${escH(v.reason)}</div>`).join("");
  }

  // equity chart
  drawPolyChart(Array.isArray(tl)&&!tl.error?tl:[]);
}

// ---- India Desk ----
async function loadIndia(){
  try{
    const r=await fetch("/api/polymarket/india",{cache:"no-store"});
    if(!r.ok) throw new Error("HTTP "+r.status);
    const d=await r.json();
    if(d.error){ $("indiaMarkets").innerHTML=`<div class="polyempty">${escH(d.error)}</div>`; return; }
    const mk=$("indiaMarkets"), tl=$("indiaTradesTable");
    $("indiaCount").textContent=d.markets.length;
    $("indiaTrades").textContent=d.recent_india_trades.length;
    $("indiaLast").textContent="data-only · no orders · "+new Date().toLocaleTimeString();
    if(!d.markets.length){
      mk.innerHTML=`<div class="polyempty">no Indian markets tracked yet — run the india_watch scanner, or add slugs to india_watchlist.json</div>`;
    }else{
      mk.innerHTML=`<table class="polytbl"><thead><tr>
        <th>Market</th><th>Last</th><th>Spread</th><th>Liquidity</th><th>Vol 24h</th><th>Last whale trade</th>
        </tr></thead><tbody>`+d.markets.map(m=>{
        let p="—";
        try{ const op=JSON.parse(m.outcome_prices||"[]"); p=op[1]!==undefined?`${(op[1]*100).toFixed(1)}¢`:(op[0]!==undefined?`${(op[0]*100).toFixed(1)}¢`:"—"); }catch(e){}
        return `<tr>
          <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escH(m.market)}">${escH(m.market)}</td>
          <td>${p}</td><td>${m.spread!=null?(m.spread*100).toFixed(1)+"¢":"—"}</td>
          <td>${m.liquidity!=null?"$"+Math.round(m.liquidity).toLocaleString():"—"}</td>
          <td>${m.volume_24h!=null?"$"+Math.round(m.volume_24h).toLocaleString():"—"}</td>
          <td style="font-size:11px">${m.last_whale_trade_at?escH(String(m.last_whale_trade_at).replace("T"," ").slice(5,16)):"—"}</td>
        </tr>`;
      }).join("")+`</tbody></table>`;
    }
    if(!d.recent_india_trades.length){
      tl.innerHTML=`<div class="polyempty">no India-relevant whale trades in the last 7 days</div>`;
    }else{
      tl.innerHTML=`<table class="polytbl"><thead><tr><th>When</th><th>Side</th><th>Price</th><th>Size</th><th>Market</th></tr></thead><tbody>`+
        d.recent_india_trades.map(t=>`<tr>
          <td style="font-size:11px">${escH(String(t.timestamp).replace("T"," ").slice(5,16))}</td>
          <td style="color:${t.side==="YES"?"var(--teal)":"var(--brick)"}">${escH(t.side)}</td>
          <td>${Number(t.price).toFixed(3)}</td><td>${Number(t.size).toFixed(2)}</td>
          <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escH(t.market)}">${escH(t.market)}</td>
        </tr>`).join("")+`</tbody></table>`;
    }
  }catch(e){ $("indiaMarkets").innerHTML=`<div class="polyempty">india desk error: ${escH(e.message)}</div>`; }
}

// ---- sectioned layout: menubar tab switcher ----
function switchTab(name){
  document.querySelectorAll(".tabpage").forEach(s=>s.classList.toggle("active",s.dataset.tab===name));
  document.querySelectorAll(".mtab").forEach(b=>b.classList.toggle("active",b.dataset.tab===name));
  try{ localStorage.setItem("deskTab",name); }catch(e){}
  if(location.hash!==("#"+name)) history.replaceState(null,"","#"+name);
  if(name==="polymarket") loadPoly();
  if(name==="india") loadIndia();
  if(name==="charts"){
    // TradingView + lightweight charts size to visible containers: give them
    // a resize kick so a tab opened hidden renders at full width.
    window.dispatchEvent(new Event("resize"));
    if(window.TradingView) buildTV(tvSymbol($("chartAsset")?.value||"BTC"));
    setTimeout(()=>window.dispatchEvent(new Event("resize")),250);
  }
  if(name==="brake"){ try{ drawEquityChart(_lastEquity); }catch(e){} }
}
document.querySelectorAll(".mtab").forEach(b=>{
  b.onclick=()=>switchTab(b.dataset.tab);
});
window.addEventListener("hashchange",()=>{
  const t=(location.hash||"").replace("#","");
  if(t&&document.querySelector(`.tabpage[data-tab="${t}"]`)) switchTab(t);
});
(function initTab(){
  let t=null;
  try{ t=localStorage.getItem("deskTab"); }catch(e){}
  const valid=["desk","charts","brake","backtests","polymarket","india","nifty"];
  if(location.hash&&valid.includes(location.hash.slice(1))) t=location.hash.slice(1);
  if(!t||!valid.includes(t)) t="desk";
  switchTab(t);
})();
setInterval(loadPoly,60000);
</script>
</body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")
