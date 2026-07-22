#!/usr/bin/env python3
"""
Trading Desk — one local dashboard for every bot.

Aggregates all three Freqtrade bots (spot / futures / braked-hold), the portfolio
guardian, and the 200-day brake board into a single auto-refreshing page served at
http://127.0.0.1:8090 . Brake states are read from the cached hourly state file so
the page never blocks on exchange calls.

Run:  ./.venv/bin/python dashboard.py     (or via launchd com.vikas.dashboard)
"""
import csv
from local_secrets import api_pw
import json
import os
import re

import requests
import uvicorn
from requests.auth import HTTPBasicAuth
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(BASE, "guard_state.json")
BRAKE_STATE = os.path.join(BASE, "brake_alert_state.json")
FUND_LOG = os.path.join(BASE, "funding_monitor.log")
EQUITY_CSV = os.path.join(BASE, "equity_history.csv")
MACRO_STATE = os.path.join(BASE, "macro_alert_state.json")
MACRO_WATCH = os.path.join(BASE, "macro_watchlist.json")
TREND_BOARD = os.path.join(BASE, "trendline_board.json")
PORTFOLIO_BOARD = os.path.join(BASE, "diversified_brake_board.json")
ACTIVITY_FEED = os.path.join(BASE, "activity_feed.jsonl")
START_EACH = 1000.0


def read_macro():
    """Cached macro brake board (stocks/bonds/gold) from the 6h macro job."""
    if not os.path.exists(MACRO_STATE):
        return []
    try:
        state = json.load(open(MACRO_STATE))
        names = {}
        if os.path.exists(MACRO_WATCH):
            for a in json.load(open(MACRO_WATCH)).get("assets", []):
                names[a["symbol"]] = a["name"]
        out = []
        for sym, d in state.items():
            price, sma = d.get("price", 0), d.get("sma", 0)
            out.append({"symbol": sym, "name": names.get(sym, sym),
                        "state": d.get("state"), "price": price,
                        "gap": ((price / sma - 1) * 100) if sma else 0})
        out.sort(key=lambda x: (x["state"] != "above", -x["gap"]))
        return out
    except Exception:
        return []


def _job_paused(job):
    """True if a board's scheduled generator is disabled (its plist was archived to
    disabled_plists/ in the 2026-07-20 simplification). These panels read CACHED files,
    so when the generator is off the panel is genuinely frozen — drives the 'frozen'
    badge. Self-clears the moment the job is re-enabled (plist back in LaunchAgents)."""
    return not os.path.exists(
        os.path.expanduser(f"~/Library/LaunchAgents/com.vikas.{job}.plist"))


def read_trend():
    """Cached trendline board (Tori's valid-line filters) from the 4h trendline job.
    Returns {updated, source, coins:[...]} or empty. Setups (bounce/break/reject)
    sort to the top; 'inside channel' coins fall below."""
    if not os.path.exists(TREND_BOARD):
        return {}
    try:
        d = json.load(open(TREND_BOARD))
        coins = d.get("coins", [])
        coins.sort(key=lambda c: (c.get("signal") is None, c.get("coin")))
        d["coins"] = coins
        d["_paused"] = _job_paused("trendline")
        return d
    except Exception:
        return {}


def read_portfolio():
    """Cached diversified-braked portfolio map (mirrors the /portfolio Telegram view)."""
    if not os.path.exists(PORTFOLIO_BOARD):
        return {}
    try:
        d = json.load(open(PORTFOLIO_BOARD))
        d["_paused"] = _job_paused("divbrake")
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
            for k in ("spot", "futures", "brakedhold", "apex", "spx", "nifty", "ongc", "itc",
                      "btc_hold", "basket_hold"):
                v = r.get(k)
                try:
                    row[k] = float(v) if v not in (None, "") else None
                except ValueError:
                    row[k] = None
            out.append(row)
    except Exception:
        pass
    return out


def funding_status():
    """Read the cached funding-carry line the daily job appends (never computes
    live — that takes ~60s). Line shape: '2026-07-17 09:14 | AVG 4.0% 1.0%'."""
    if not os.path.exists(FUND_LOG):
        return None
    try:
        lines = [l for l in open(FUND_LOG).read().splitlines() if l.strip()]
        if not lines:
            return None
        last = lines[-1]
        when = last.split("|")[0].strip()
        nums = re.findall(r"([\d.]+)%", last)
        if len(nums) < 2:
            return None
        gross, net = float(nums[0]), float(nums[1])
        verdict = "STRONG" if gross >= 20 else "MODEST" if gross >= 10 else "SKIP"
        return {"gross": gross, "net": net, "verdict": verdict, "when": when}
    except Exception:
        return None

BOTS = [
    ("Spot", 8080, api_pw(8080), "Trend-follow · spot"),
    ("Futures", 8081, api_pw(8081), "Long / short · futures"),
    ("Braked Hold", 8082, api_pw(8082), "200-day brake · spot"),
    ("Scalp", 8083, api_pw(8083), "VWAP reversion · 5m LAB"),
    ("Day Trade", 8084, api_pw(8084), "Opening-range breakout · 1h LAB"),
    ("ApeX", 8085, api_pw(8085), "ApeX Omni · DEX perps"),
    ("S&P 500", 8086, api_pw(8086), "SMA cross · SPY paper"),
    ("Nifty 50", 8087, api_pw(8087), "SMA cross · NIFTYBEES paper"),
    ("ONGC", 8088, api_pw(8088), "Dividend · ONGC 7.4%"),
    ("ITC", 8089, api_pw(8089), "Dividend · ITC 5.7%"),
]

app = FastAPI(title="Trading Desk")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")

CRYPTO_COINS = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC",
                "DOGE", "LINK", "BNB", "AVAX", "DOT", "TRX"]


@app.get("/api/candles")
def candles(asset: str):
    """OHLCV daily candles + 200-day line for one asset. Crypto comes from the
    braked-hold bot (sma200 pre-computed); macro comes from yfinance."""
    a = asset.upper()
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


@app.get("/api/overview")
def overview():
    bots, tot_bal = [], 0.0
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
                opens.append({
                    "pair": t["pair"],
                    "dir": "SHORT" if t.get("is_short") else "LONG",
                    "profit": (t.get("profit_ratio") or 0) * 100,
                    "stake": t.get("stake_amount") or 0,
                })
        bt = bal.get("total")
        has_bal = isinstance(bt, (int, float)) and not isinstance(bt, bool)
        b = bt if has_bal else 0
        if has_bal:
            tot_bal += b
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
            "open": opens, "equity": eq,
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
    n_reporting = sum(1 for x in bots if x.get("has_balance"))
    start_total = START_EACH * n_reporting
    missing = [x["name"] for x in bots if not x.get("has_balance")]
    return JSONResponse({
        "bots": bots,
        "total_balance": tot_bal,
        "total_pnl_pct": (tot_bal / start_total - 1) * 100 if start_total else 0,
        "bots_reporting": n_reporting, "bots_missing": missing,
        "guardian": {
            "tripped": bool(g.get("breaker_tripped", False)),
            "paused": bool(g.get("paused_by_guard", False)),
            "peak": g.get("peak_balance", 0) or 0,
        },
        "brake": brake, "brake_hold": hold, "brake_total": len(brake),
        "macro": read_macro(),
        "trend": read_trend(),
        "portfolio": read_portfolio(),
        "activity": read_activity(),
        "memory": mem,
        "funding": funding_status(),
        "equity_history": read_equity(),
    })


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


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Desk</title>
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
  .deskrail .macroboard{grid-template-columns:1fr 1fr;}
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

  .bots{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin-bottom:30px;}
  .bot{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:18px;display:flex;flex-direction:column;gap:14px;}
  .bot .top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;}
  .bot .nm{font-weight:700;font-size:16px;letter-spacing:-.01em;}
  .bot .ds{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:2px;}
  .state{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
    padding:4px 9px;border-radius:20px;border:1px solid var(--line);white-space:nowrap;}
  .state.run{color:var(--teal);border-color:rgba(63,199,168,.4);}
  .state.paused{color:var(--amber);border-color:rgba(240,168,60,.4);}
  .state.off{color:var(--brick);border-color:rgba(229,103,78,.4);}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px 10px;}
  .metric .label{display:block;margin-bottom:4px;}
  .metric .v{font-family:var(--mono);font-size:19px;font-weight:700;}
  .sparkwrap{border-top:1px solid var(--line);padding-top:11px;}
  .sparkwrap .label{display:block;margin-bottom:7px;}
  svg.spark{width:100%;height:40px;display:block;}
  .sparkempty{font-family:var(--mono);font-size:11px;color:var(--faint);}
  .opens{border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:7px;}
  .opens .none{font-family:var(--mono);font-size:12px;color:var(--faint);}
  .pos-row{display:flex;justify-content:space-between;font-family:var(--mono);font-size:12.5px;gap:8px;}
  .pos-row .p{color:var(--muted);}
  .tag{font-family:var(--mono);font-size:9.5px;padding:1px 5px;border-radius:4px;letter-spacing:.05em;}
  .tag.long{background:rgba(63,199,168,.16);color:var(--teal);}
  .tag.short{background:rgba(229,103,78,.16);color:var(--brick);}
  /* --- bot-card visual upgrades --- */
  .bot.up{box-shadow:inset 3px 0 0 var(--teal);}
  .bot.down{box-shadow:inset 3px 0 0 var(--brick);}
  .bot.flat{box-shadow:inset 3px 0 0 var(--muted);}
  .metric .v{font-variant-numeric:tabular-nums;}
  .state .livedot{display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor;margin-right:6px;vertical-align:middle;}
  .state.run .livedot{animation:livepulse 1.8s ease-in-out infinite;}
  @keyframes livepulse{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.35;transform:scale(.82);}}
  @media(prefers-reduced-motion:reduce){.state.run .livedot{animation:none;}}
  .watching{border-top:1px solid var(--line);margin-top:11px;padding-top:9px;display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;}
  .watching .wl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-right:4px;}
  .watching .wc{font-family:var(--mono);font-size:11.5px;color:var(--muted);}
  .watching .wc.on{color:var(--teal);font-weight:600;}
  .watching .wsep{color:var(--faint);font-size:10px;}
  .winbar{height:4px;border-radius:2px;background:var(--line);margin-top:7px;max-width:76px;overflow:hidden;}
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
  .macroboard{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:9px;}
  .macroboard .coin .c{font-size:13px;}
  @media(max-width:520px){.macroboard{grid-template-columns:1fr 1fr;}}
  .trendboard{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:9px;}
  .trendboard .coin .c{font-size:13px;}
  @media(max-width:520px){.trendboard{grid-template-columns:1fr;}}
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
  @media(max-width:900px){.bots{grid-template-columns:1fr;}.board{grid-template-columns:repeat(3,1fr);}
    .totals{gap:22px;}.totals .big{font-size:21px;}}
  @media(max-width:520px){.board{grid-template-columns:repeat(2,1fr);}}
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
      <div class="t"><span class="label">Total P&amp;L</span>
        <span class="big" id="tpnl">—</span></div>
      <div class="t"><span class="label">Status</span>
        <span class="live"><i class="dot pulse" id="livedot"></i><span id="upd">connecting…</span></span></div>
    </div>
  </header>

  <div class="toolbar">
    <button class="tbtn" id="btnBrake" type="button">🪂 Brake — live check</button>
    <button class="tbtn" id="btnMem" type="button">🧠 Memory — full record</button>
    <button class="tbtn" id="themeToggle" type="button" style="margin-left:auto">☀️ Light mode</button>
  </div>

  <div id="guard" class="guard" hidden></div>

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

  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">200-day brake board · crypto</h2>
    <span class="label" id="brakesum"></span>
  </div>
  <div class="board" id="board"></div>

  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">Macro brake · stocks / bonds / gold</h2>
    <span class="label" id="macrosum"></span>
  </div>
  <div class="board macroboard" id="macroboard"></div>

  <div class="brakehead" style="margin-top:26px">
    <h2 class="sec">Trendline setups · Filters</h2>
    <span class="label" id="trendsum"></span>
  </div>
  <div class="board trendboard" id="trendboard"></div>

  <div class="panelrow">
    <div>
      <h2 class="sec">Equity vs buy &amp; hold</h2>
      <div class="eqwrap">
        <div class="eqleg">
          <span><i style="background:var(--amber)"></i>Braked Hold</span>
          <span><i style="background:#6BA5E0"></i>Spot</span>
          <span><i style="background:#A98BE0"></i>Futures</span>
          <span><i style="background:#f5a623"></i>ApeX</span>
          <span><i style="background:#4ADE80"></i>S&amp;P 500</span>
          <span><i style="background:#FF7AB6"></i>Nifty 50</span>
          <span><i style="background:#FFB347"></i>ONGC</span>
          <span><i style="background:#9ACD32"></i>ITC</span>
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
    <div>
      <h2 class="sec">Funding carry · yield</h2>
      <div class="fund" id="funding"></div>
    </div>
  </div>

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
let _chart=null,_cs=null,_ss=null,_vs=null,_data=null,_range="1Y",_tall=false;
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
const money=v=>"$"+(v>=1000?v.toLocaleString(undefined,{maximumFractionDigits:0}):v.toFixed(2));
const pct=v=>(v>=0?"+":"")+v.toFixed(2)+"%";
const cls=v=>v>0?"pos":v<0?"neg":"";
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
    {k:"apex",color:"#f5a623",lw:1.5,dash:[]},
    {k:"spx",color:"#4ADE80",lw:1.5,dash:[]},
    {k:"nifty",color:"#FF7AB6",lw:1.5,dash:[]},
    {k:"ongc",color:"#FFB347",lw:1.5,dash:[]},
    {k:"itc",color:"#9ACD32",lw:1.5,dash:[]},
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

let tickBusy=false;             // in-flight guard: a hung bot must not stack requests
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
    $("upd").textContent="live · "+new Date().toLocaleTimeString();

    // guardian
    const g=$("guard"); g.hidden=false;
    g.className="guard"+(d.guardian.tripped?" tripped":"");
    const gcol=d.guardian.tripped?"var(--brick)":d.guardian.paused?"var(--amber)":"var(--teal)";
    const gtxt=d.guardian.tripped?"CIRCUIT BREAKER TRIPPED — halted":d.guardian.paused?"paused by guardian":"armed · watching";
    g.innerHTML=`<span class="chip"><i class="s" style="background:${gcol}"></i>🛡️ Guardian: <b>${gtxt}</b></span>`
      +`<span style="color:var(--muted)">peak equity ${money(d.guardian.peak)}</span>`;

    // bots
    $("bots").innerHTML=d.bots.map(b=>{
      const sc=!b.online?"off":b.state==="running"?"run":"paused";
      const stxt=!b.online?"offline":b.state;
      const health=!b.online?"":(b.profit_pct>0.001?"up":b.profit_pct<-0.001?"down":"flat");
      const parrow=b.profit_pct>0.001?"▲ ":b.profit_pct<-0.001?"▼ ":"";
      const wr=b.trades?Math.round(b.winrate):0;
      const opens=b.open.length?b.open.map(o=>
        `<div class="pos-row"><span><i class="dot" style="background:${o.profit>=0?'var(--teal)':'var(--brick)'}"></i>${o.pair} <span class="tag ${o.dir.toLowerCase()}">${o.dir}</span></span>`
        +`<span class="${cls(o.profit)}">${pct(o.profit)} <span class="p">·${money(o.stake)}</span></span></div>`).join("")
        :`<div class="none">no open positions</div>`;
      const openBases=new Set(b.open.map(o=>o.pair.split('/')[0]));
      const watch=(b.watching&&b.watching.length)?
        `<div class="watching"><span class="wl">Watching ${b.watching.length}</span>`
        +b.watching.map(c=>`<span class="wc${openBases.has(c)?' on':''}">${c}${openBases.has(c)?' ●':''}</span>`).join('<span class="wsep">·</span>')
        +`</div>`:"";
      return `<div class="bot ${health}"><div class="top">
          <div><div class="nm">${b.name}</div><div class="ds">${b.desc}</div></div>
          <span class="state ${sc}"><i class="livedot"></i>${stxt}</span></div>
        <div class="grid2">
          <div class="metric"><span class="label">Wallet</span><span class="v">${money(b.balance)}</span></div>
          <div class="metric"><span class="label">Closed P&L</span><span class="v ${cls(b.profit_pct)}">${parrow}${pct(b.profit_pct)}</span></div>
          <div class="metric"><span class="label">Trades</span><span class="v">${b.trades}</span></div>
          <div class="metric"><span class="label">Win rate</span><span class="v">${b.trades?wr+"%":"—"}</span>${b.trades?`<div class="winbar"><i style="width:${Math.min(100,wr)}%"></i></div>`:""}</div>
        </div>
        <div class="sparkwrap"><span class="label">30-day equity</span>${spark(b.equity)}</div>
        <div class="opens">${opens}</div>${watch}</div>`;
    }).join("");

    // brake board
    $("brakesum").textContent=d.brake_total?`${d.brake_hold} holding · ${d.brake_total-d.brake_hold} in cash`:"";
    $("board").innerHTML=d.brake.length?d.brake.map(c=>{
      const hold=c.state==="above";
      return `<div class="coin ${hold?"hold":"cash"}">
        <div class="c"><i class="s" style="background:${hold?"var(--teal)":"var(--brick)"}"></i>${c.coin}</div>
        <div class="g ${hold?"pos":"neg"}">${hold?"HOLD":"CASH"} ${(c.gap>=0?"+":"")+c.gap.toFixed(0)}%</div>
        <div class="g" style="color:var(--faint)">${price(c.price)}</div></div>`;
    }).join(""):`<div class="none mono" style="color:var(--faint)">brake state not yet cached — the hourly job populates it</div>`;

    // macro brake board
    const macro=d.macro||[];
    $("macrosum").textContent=macro.length?`${macro.filter(x=>x.state==="above").length} holding · ${macro.filter(x=>x.state!=="above").length} in cash`:"";
    $("macroboard").innerHTML=macro.length?macro.map(c=>{
      const hold=c.state==="above";
      return `<div class="coin ${hold?"hold":"cash"}">
        <div class="c"><i class="s" style="background:${hold?"var(--teal)":"var(--brick)"}"></i>${c.name}</div>
        <div class="g ${hold?"pos":"neg"}">${hold?"HOLD":"CASH"} ${(c.gap>=0?"+":"")+c.gap.toFixed(0)}%</div></div>`;
    }).join(""):`<div class="none mono" style="color:var(--faint)">macro state not yet cached — the 6h job populates it</div>`;

    // shared "frozen" badge: these panels read CACHED board files, so when the
    // generating job is paused (2026-07-20 simplification) the panel is truly stale.
    const fbadge=(iso)=>{
      let w="";
      if(iso){w=" · last updated "+String(iso).slice(0,16).replace("T"," ")+" UTC";}
      return `<div class="mono" style="font-size:11px;color:var(--amber);`
        +`border:1px solid rgba(240,168,60,.35);border-radius:6px;padding:4px 8px;margin-bottom:8px">`
        +`⏸ FROZEN — auto-monitor paused${w}. For a live reading use Telegram /trend · /portfolio</div>`;};

    // trendline board (Tori's valid-line filters)
    const tb=(d.trend&&d.trend.coins)||[];
    const setups=tb.filter(c=>c.signal);
    const meta={BOUNCE_LONG:["🟢","bounce","var(--teal)","pos"],
                BREAK_UP:["🚀","breakout","var(--teal)","pos"],
                BREAK_DOWN:["🔴","breakdown","var(--brick)","neg"],
                REJECT_SHORT:["⚠️","rejected","var(--amber)","neg"]};
    $("trendsum").textContent=tb.length?`${setups.length} setup${setups.length===1?"":"s"} · ${tb.length-setups.length} inside channel`:"";
    const tfrozen=(d.trend&&d.trend._paused)?fbadge(d.trend.updated):"";
    $("trendboard").innerHTML=tfrozen+(tb.length?tb.map(c=>{
      if(!c.signal){
        return `<div class="coin" style="opacity:.55">
          <div class="c"><i class="s" style="background:var(--faint)"></i>${c.coin}</div>
          <div class="g" style="color:var(--faint)">inside channel</div>
          <div class="g" style="color:var(--faint)">${price(c.close)}</div></div>`;}
      const mt=meta[c.signal]||["•","",'var(--faint)',""];
      return `<div class="coin" style="border-left:3px solid ${mt[2]}">
        <div class="c"><i class="s" style="background:${mt[2]}"></i>${c.coin} <span style="color:var(--faint);font-weight:600">${mt[0]} ${mt[1]}</span></div>
        <div class="g ${mt[3]}">${price(c.entry)} → ${price(c.target)} <span style="color:var(--faint)">(${c.rr}R)</span></div>
        <div class="g" style="color:var(--faint)">stop ${price(c.stop)} · ${c.touches} touches</div></div>`;
    }).join(""):`<div class="none mono" style="color:var(--faint)">trendline board not yet cached — the 4h job populates it</div>`);

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
    drawEquityChart(d.equity_history);

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

    // funding carry
    const f=d.funding;
    if(f){
      const vc=f.verdict.toLowerCase();
      const hint=f.verdict==="STRONG"?"Attractive — market-neutral yield worth considering."
        :f.verdict==="MODEST"?"Middling — only worth it with idle capital."
        :"Not worth deploying — carry is compressed right now.";
      $("funding").innerHTML=
        `<span class="verdict ${vc}">${f.verdict}</span>`
        +`<div class="fm"><span class="label">Gross carry</span><span class="v">${f.gross.toFixed(1)}%</span></div>`
        +`<div class="fm"><span class="label">Net (after costs)</span><span class="v ${f.net>0?'pos':''}">${f.net.toFixed(1)}%</span></div>`
        +`<div class="fm"><span class="label">As of</span><span class="v" style="font-size:13px">${f.when}</span></div>`
        +`<span class="hint">${hint} Checked daily 9am.</span>`;
    } else {
      $("funding").innerHTML=`<span class="none mono" style="color:var(--faint)">no funding reading yet — the daily 9am job populates it</span>`;
    }
  }catch(e){
    $("err").hidden=false; $("err").textContent="⚠️ can't reach the desk API: "+e.message;
    $("upd").textContent="disconnected"; $("livedot").style.background="var(--brick)";
  }finally{
    tickBusy=false;
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
</script>
</body></html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="warning")
