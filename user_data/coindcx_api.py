"""Low-level CoinDCX REST client — the only place that speaks CoinDCX's wire format.

WHY this exists (audit 2026-07-28): CoinDCX is not in ccxt (4.5.65) and the
`coindcx` PyPI package does not import (not installed / broken), so every other
module in the stack would otherwise hand-roll requests against an API whose
quirks are easy to get wrong. Those quirks, all verified live against the API:

  * Pair ids are `<ecode>-<target>_<quote>` ("B-SOL_USDT"), NOT "SOL/USDT:USDT".
    `ecode` "B" means the book is Binance-mirrored.
  * Candles come back NEWEST-FIRST as dicts (`time` in ms), not ccxt's
    oldest-first lists — every consumer forgetting this silently backtests on
    reversed data.
  * `limit` is hard-capped at 1000 (HTTP 422 above it), so history needs
    startTime/endTime windowing.
  * Only intervals [1m, 15m, 1h, 1d] exist. There is NO 4h — callers that want
    4h must resample 1h (see `resample`).
  * /market_data/candles serves SPOT candles only. It rejects futures-only pairs
    (HTTP 422 "Invalid pair B-1000PEPE_USDT") and silently ignores a
    `contract=futures` param. There is no public futures OHLCV endpoint.
  * Funding is realtime-only (`fr` on the futures ticker). No history endpoint
    exists, so funding cannot be backtested — see `futures_realtime`.

Every network read is wrapped and returns None/[]/{} on failure so daemons and
download loops can degrade instead of dying.
"""

import hashlib
import hmac
import json
import time

import requests

PUBLIC = "https://public.coindcx.com"
API = "https://api.coindcx.com"

TIMEOUT = 20  # seconds; CoinDCX p99 is well under this
MAX_LIMIT = 1000  # hard server cap — HTTP 422 above it
RATE_SLEEP = 0.15  # seconds between paged calls; no documented limit, be polite

# The only intervals the candle endpoint accepts (422 lists them on anything else).
INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}

# Order book depths that actually return levels; anything else 200s with {} .
ORDERBOOK_DEPTHS = (10, 20, 50)


def _get(url, params=None, retries=3):
    """GET → parsed JSON, or None. Never raises — callers isinstance-check."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            # 422/400 are permanent (bad pair/interval) — retrying just burns time.
            if r.status_code in (400, 404, 422):
                return None
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    return None


# ---- SYMBOL MAPPING ----
# Freqtrade wants "SOL/USDT:USDT"; CoinDCX wants "B-SOL_USDT". Kept here so the
# ccxt shim and the downloader cannot drift apart.

def ft_to_dcx(symbol, ecode="B"):
    """'SOL/USDT:USDT' -> 'B-SOL_USDT'."""
    base = symbol.split("/")[0]
    quote = symbol.split("/")[1].split(":")[0]
    return f"{ecode}-{base}_{quote}"


def dcx_to_ft(pair):
    """'B-SOL_USDT' -> 'SOL/USDT:USDT'. Returns None on an unparseable id."""
    try:
        _ecode, rest = pair.split("-", 1)
        base, quote = rest.rsplit("_", 1)
        return f"{base}/{quote}:{quote}"
    except ValueError:
        return None


# ---- MARKET METADATA ----

def markets_details():
    """All spot markets (min_quantity, precision, step, status). [] on failure."""
    d = _get(f"{API}/exchange/v1/markets_details")
    return d if isinstance(d, list) else []


def active_futures_pairs(margin="INR"):
    """Futures pair ids tradable against `margin`. INR-margined = no TDS."""
    d = _get(
        f"{API}/exchange/v1/derivatives/futures/data/active_instruments",
        {"margin_currency_short_name[]": margin},
    )
    return d if isinstance(d, list) else []


def futures_instrument(pair, margin="INR"):
    """Real futures contract spec — leverage, increments, fees, funding_frequency.

    This is the authoritative source for fees: maker_fee/taker_fee are quoted in
    PERCENT (0.059 == 0.059% == 0.00059 as a fraction). Do not use the spot
    markets_details record for a futures contract — its max_leverage is 0.
    """
    d = _get(
        f"{API}/exchange/v1/derivatives/futures/data/instrument",
        {"margin_currency_short_name[]": margin, "pair": pair},
    )
    if isinstance(d, dict) and isinstance(d.get("instrument"), dict):
        return d["instrument"]
    return None


# ---- MARKET DATA ----

def candles(pair, interval="1h", start_ms=None, end_ms=None, limit=MAX_LIMIT):
    """One raw candle page, normalised to ccxt's [ts, o, h, l, c, v] ASCENDING.

    The endpoint answers newest-first; we reverse so callers never have to think
    about it. Returns [] on failure or unknown interval.
    """
    if interval not in INTERVAL_MS:
        return []
    params = {"pair": pair, "interval": interval, "limit": min(int(limit), MAX_LIMIT)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    d = _get(f"{PUBLIC}/market_data/candles", params)
    if not isinstance(d, list):
        return []
    out = []
    for c in d:
        try:
            out.append([int(c["time"]), float(c["open"]), float(c["high"]),
                        float(c["low"]), float(c["close"]), float(c["volume"])])
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed rows rather than abort the whole page
    out.sort(key=lambda r: r[0])
    return out


def candles_range(pair, interval, since_ms, until_ms=None, progress=None):
    """Full history over [since_ms, until_ms] by walking 1000-candle windows.

    Server caps `limit` at 1000, so we advance a window of 1000*interval and
    stitch. Dedups by timestamp (windows can overlap at the seam) and stops when
    a window comes back empty AND we are past the last known candle — an early
    empty window just means the pair had not listed yet, so we keep probing
    forward instead of truncating history.
    """
    step = INTERVAL_MS.get(interval)
    if not step:
        return []
    until_ms = int(until_ms if until_ms is not None else time.time() * 1000)
    window = step * (MAX_LIMIT - 1)
    seen, rows = set(), []
    cursor = int(since_ms)
    empty_streak = 0
    while cursor < until_ms:
        page = candles(pair, interval, cursor, min(cursor + window, until_ms))
        new = [r for r in page if r[0] not in seen]
        for r in new:
            seen.add(r[0])
        rows.extend(new)
        # 10 consecutive empty windows (~10k candles) means the listing really
        # has not started yet; bail only once that is implausible.
        empty_streak = 0 if page else empty_streak + 1
        if empty_streak >= 10:
            break
        if progress:
            progress(pair, interval, cursor, len(rows))
        cursor += window + step
        time.sleep(RATE_SLEEP)
    rows.sort(key=lambda r: r[0])
    return rows


def resample(rows, src="1h", dst="4h"):
    """Aggregate ascending OHLCV rows to a coarser timeframe.

    Needed because CoinDCX has no 4h interval but SolRSI2_1h uses a 4h
    informative. Buckets are floor-aligned to the epoch, which matches how every
    exchange stamps 4h candles (00/04/08/12/16/20 UTC), so the resampled series
    is identical to a native one as long as no source candle is missing. Drops a
    trailing partial bucket so backtests never see a half-formed candle.
    """
    src_ms, dst_ms = INTERVAL_MS.get(src), INTERVAL_MS.get(dst, 4 * 3_600_000)
    if not src_ms or dst_ms % src_ms:
        return []
    per = dst_ms // src_ms
    buckets = {}
    for ts, o, h, low, c, v in rows:
        b = ts - (ts % dst_ms)
        cur = buckets.get(b)
        if cur is None:
            buckets[b] = [b, o, h, low, c, v, 1]
        else:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], low)
            cur[4] = c
            cur[5] += v
            cur[6] += 1
    out = [b[:6] for b in sorted(buckets.values(), key=lambda r: r[0])]
    if out and buckets[out[-1][0]][6] < per:
        out.pop()  # trailing bucket still forming
    return out


def futures_realtime():
    """Realtime futures snapshot keyed by pair id.

    Per pair: `ls` last, `mp` mark, `fr` funding rate (fraction, per 8h), `efr`
    estimated next funding, `h`/`l`/`v` 24h stats. This is the ONLY funding data
    CoinDCX exposes — there is no history endpoint, so funding fees can be paid
    live but cannot be backtested.
    """
    d = _get(f"{PUBLIC}/market_data/v3/current_prices/futures/rt")
    if isinstance(d, dict) and isinstance(d.get("prices"), dict):
        return d["prices"]
    return {}


def spot_tickers():
    """Spot ticker array keyed by coindcx_name ('SOLUSDT'). {} on failure."""
    d = _get(f"{API}/exchange/ticker")
    if not isinstance(d, list):
        return {}
    return {t["market"]: t for t in d if isinstance(t, dict) and "market" in t}


def orderbook(pair, depth=50):
    """Futures order book. CoinDCX returns {price: qty} DICTS, not ccxt lists.

    Returns ccxt-shaped {'bids': [[p, q], ...], 'asks': [...]} with bids
    descending / asks ascending, because the raw dict has no meaningful order.

    TRAP (verified 2026-07-28): only depths 10/20/50 are real. Depth 5 or 100
    returns HTTP 200 with an EMPTY book rather than an error, which would look
    like a vanished market to anything reading the top of book — so we snap to
    the nearest supported depth instead of passing the caller's value through.
    """
    depth = min(ORDERBOOK_DEPTHS, key=lambda d: abs(d - int(depth)))
    d = _get(f"{PUBLIC}/market_data/v3/orderbook/{pair}-futures/{depth}")
    if not isinstance(d, dict):
        return {"bids": [], "asks": [], "timestamp": None}

    def side(key, reverse):
        raw = d.get(key)
        if not isinstance(raw, dict):
            return []
        lvls = []
        for p, q in raw.items():
            try:
                lvls.append([float(p), float(q)])
            except (TypeError, ValueError):
                continue
        lvls.sort(key=lambda x: x[0], reverse=reverse)
        return lvls

    return {"bids": side("bids", True), "asks": side("asks", False),
            "timestamp": d.get("ts")}


# ---- AUTHENTICATED (UNVERIFIED — no API keys were available at build time) ----
# Everything below is written to CoinDCX's documented signing scheme but has
# NEVER been executed against a real account. The bot runs dry_run=true, so
# freqtrade never reaches these paths. Treat as a starting point, not as tested
# code: place one manual minimum-size order and compare the response before
# trusting it with capital.
#
# Signing: HMAC-SHA256 of the EXACT JSON body bytes, keyed by the api secret.
# The body must be sent byte-identical to what was signed (hence one dumps()).

# margin_currency_short_name="INR" is what makes a position INR-margined, which
# is the entire tax reason for choosing CoinDCX (no TDS, slab rate vs 30%).
# Passing "USDT" here silently opens a USDT-margined position instead.
MARGIN_CURRENCY = "INR"


def signed_post(path, body, key, secret, timeout=TIMEOUT):
    """POST a signed private request. Returns parsed JSON, or None on failure."""
    if not key or not secret:
        return None
    body = dict(body or {})
    body["timestamp"] = int(time.time() * 1000)
    payload = json.dumps(body, separators=(",", ":"))
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": key,
        "X-AUTH-SIGNATURE": sig,
    }
    try:
        r = requests.post(f"{API}{path}", data=payload, headers=headers, timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def futures_wallets(key, secret):
    """Margin wallet balances (INR + any USDT). [] on failure."""
    d = signed_post("/exchange/v1/derivatives/futures/wallets", {}, key, secret)
    return d if isinstance(d, list) else []


def futures_positions(key, secret, page="1", size="100"):
    """Open futures positions. [] on failure."""
    d = signed_post(
        "/exchange/v1/derivatives/futures/positions",
        {"page": page, "size": size, "margin_currency_short_name": [MARGIN_CURRENCY]},
        key, secret,
    )
    return d if isinstance(d, list) else []


def create_futures_order(pair, side, qty, key, secret, price=None, leverage=1,
                         order_type="market_order", client_id=None):
    """Place an INR-margined futures order. side is 'buy'/'sell'."""
    order = {
        "side": side,
        "pair": pair,
        "order_type": order_type,
        "total_quantity": float(qty),
        "leverage": float(leverage),
        "margin_currency_short_name": MARGIN_CURRENCY,
    }
    if price is not None:
        order["price"] = float(price)
    if client_id:
        order["client_order_id"] = client_id
    return signed_post(
        "/exchange/v1/derivatives/futures/orders/create", {"order": order}, key, secret
    )


def cancel_futures_order(order_id, key, secret):
    """Cancel one order by CoinDCX order id."""
    return signed_post(
        "/exchange/v1/derivatives/futures/orders/cancel", {"id": order_id}, key, secret
    )


def fetch_futures_order(order_id, key, secret):
    """Fetch one order by id. CoinDCX answers with a list; we unwrap to a dict."""
    d = signed_post(
        "/exchange/v1/derivatives/futures/orders",
        {"id": order_id, "margin_currency_short_name": [MARGIN_CURRENCY]}, key, secret
    )
    if isinstance(d, list):
        return d[0] if d else None
    return d if isinstance(d, dict) else None
