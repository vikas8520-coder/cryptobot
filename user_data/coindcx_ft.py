"""Freqtrade exchange adapter for CoinDCX INR-margined futures.

WHY this is not shaped like `delta_india_ft.py` (audit 2026-07-28): the Delta
adapter is a 40-line subclass because ccxt already ships a `delta` exchange and
does all the wire work. ccxt 4.5.65 has NO `coindcx`, so freqtrade's
`_init_ccxt` — which does `getattr(ccxt, name.lower())` after checking
`name in ccxt.exchanges` — has nothing to find. Subclassing freqtrade's
`Exchange` alone would fail at construction.

So this module supplies BOTH halves:
  1. a minimal ccxt-compatible exchange (sync + async) registered into the ccxt
     namespaces, translating CoinDCX's wire format to ccxt-unified structures;
  2. the `Coindcx` freqtrade subclass injected into `freqtrade.exchange`.

Registration happens at import, and it must happen BEFORE freqtrade builds its
exchange. Freqtrade has no user-module hook early enough (strategies load after
the exchange), so run everything through `scripts/ft_coindcx.py`, which imports
this first and then hands off to freqtrade's CLI.

Deliberate deviations from the original spec, each forced by the live API:
  * ohlcv_candle_limit is 1000, not 2000 — the server hard-422s above 1000.
  * The 4h informative that SolRSI2_1h needs does not exist upstream; `fetch_ohlcv`
    transparently resamples it from 1h so live/dry-run behaves like backtest.
  * Fees come from the futures instrument endpoint (0.059% taker), not the 0.05%
    the spec assumed.

LIMITATION, stated plainly: CoinDCX publishes no futures OHLCV and no funding
history. OHLCV here is the SPOT book (Binance-mirrored, same `ecode` "B" venue
the perp prices off), and funding is realtime-only. Backtests therefore price
off spot and model zero funding.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import ccxt
import ccxt.async_support as ccxt_async
import freqtrade.exchange as fx
from ccxt.base.decimal_to_precision import TICK_SIZE
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas

try:
    from user_data import coindcx_api as api
except ImportError:  # when user_data/ itself is on sys.path
    import coindcx_api as api

# Timeframes freqtrade may ask for that CoinDCX does not serve, mapped to the
# native interval we aggregate them from. 4h is the one that matters (the
# SolRSI2_1h informative); the rest come free from the same code path.
DERIVED_TF = {
    "5m": "1m", "30m": "15m", "2h": "1h", "4h": "1h",
    "6h": "1h", "8h": "1h", "12h": "1h",
}
TF_MS = dict(api.INTERVAL_MS, **{
    "5m": 300_000, "30m": 1_800_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
})


class _CoindcxCore:
    """Wire-format translation shared by the sync and async ccxt classes.

    Pure/blocking helpers only — the async subclass runs each one in a worker
    thread rather than duplicating it against aiohttp, so there is exactly one
    implementation of every CoinDCX quirk (all of which live in coindcx_api).
    """

    def describe(self):
        return self.deep_extend(super().describe(), {
            "id": "coindcx",
            "name": "CoinDCX",
            "countries": ["IN"],
            "rateLimit": 200,
            "version": "v1",
            "certified": False,
            "pro": False,
            # TICK_SIZE because CoinDCX quotes price_increment/quantity_increment
            # as step sizes (0.01), not as decimal-place counts.
            "precisionMode": TICK_SIZE,
            "has": {
                "CORS": False, "spot": False, "margin": False, "swap": True,
                "future": False, "option": False,
                "fetchMarkets": True, "fetchOHLCV": True, "fetchTicker": True,
                "fetchTickers": True, "fetchOrderBook": True,
                "fetchL2OrderBook": True, "fetchBalance": True,
                "createOrder": True, "createLimitOrder": True,
                "createMarketOrder": True, "cancelOrder": True,
                "fetchOrder": True, "fetchPositions": True,
                "setLeverage": True, "setMarginMode": True,
                # Explicitly false — see module docstring. Advertising these
                # would make freqtrade request data the API cannot produce.
                "fetchFundingRateHistory": False, "fetchFundingRates": True,
                "fetchFundingRate": True, "fetchTrades": False,
                "fetchMyTrades": False, "fetchOrders": False,
                "fetchOpenOrders": False, "fetchClosedOrders": False,
                "fetchLeverageTiers": False, "watchOHLCV": False,
            },
            "timeframes": {tf: tf for tf in list(api.INTERVAL_MS) + list(DERIVED_TF)},
            "urls": {
                "api": {"public": api.PUBLIC, "private": api.API},
                "www": "https://coindcx.com",
                "doc": "https://docs.coindcx.com/",
            },
            "options": {"defaultType": "swap", "marginCurrency": api.MARGIN_CURRENCY},
        })

    # ---- MARKETS ----

    def _build_markets(self):
        """INR-margined perpetuals, as ccxt-unified market dicts.

        Cross-references two endpoints on purpose: `active_instruments` says
        which pairs are tradable with INR margin, while the per-pair instrument
        record carries the real contract spec. The spot `markets_details` record
        must NOT be used here — its max_leverage is 0 for every pair.
        """
        pairs = api.active_futures_pairs(api.MARGIN_CURRENCY)
        # Only pairs that also have spot candles are usable: OHLCV is the spot
        # feed, so a futures-only pair would load but never produce history.
        spot_ok = {m.get("pair") for m in api.markets_details()
                   if m.get("status") == "active"}
        wanted = [p for p in pairs if p in spot_ok and api.dcx_to_ft(p)]

        # The spec endpoint is per-pair, so ~300 round trips. Sequentially that
        # is ~90s on every markets reload; pooled it is a few seconds. Failures
        # are dropped rather than retried — a pair we cannot spec is a pair we
        # must not size orders for.
        def spec(pair):
            inst = api.futures_instrument(pair, api.MARGIN_CURRENCY)
            if not inst or inst.get("status") != "active":
                return None
            return self._parse_market(pair, api.dcx_to_ft(pair), inst)

        with ThreadPoolExecutor(max_workers=12) as pool:
            return [m for m in pool.map(spec, wanted) if m]

    def _parse_market(self, pair, symbol, inst):
        base, rest = symbol.split("/")
        quote = rest.split(":")[0]
        lev = float(inst.get("max_leverage_long") or 1)
        return {
            "id": pair, "symbol": symbol,
            "base": base, "quote": quote, "settle": quote,
            "baseId": base, "quoteId": quote, "settleId": quote,
            "type": "swap", "spot": False, "margin": False, "swap": True,
            "future": False, "option": False, "contract": True,
            "linear": not inst.get("is_inverse", False),
            "inverse": bool(inst.get("is_inverse", False)),
            "active": True,
            "contractSize": float(inst.get("unit_contract_value") or 1),
            "expiry": None, "expiryDatetime": None, "strike": None,
            "optionType": None,
            # CoinDCX quotes fees in percent; ccxt/freqtrade want fractions.
            "taker": float(inst.get("taker_fee") or 0) / 100,
            "maker": float(inst.get("maker_fee") or 0) / 100,
            "percentage": True, "tierBased": False,
            "precision": {
                "price": float(inst.get("price_increment") or 0.01),
                "amount": float(inst.get("quantity_increment") or 0.01),
            },
            "limits": {
                "leverage": {"min": 1.0, "max": lev},
                "amount": {"min": float(inst.get("min_quantity") or 0),
                           "max": float(inst.get("max_quantity") or 0) or None},
                "price": {"min": float(inst.get("min_price") or 0),
                          "max": float(inst.get("max_price") or 0) or None},
                "cost": {"min": float(inst.get("min_notional") or 0),
                         "max": float(inst.get("max_notional") or 0) or None},
            },
            "info": inst,
        }

    # ---- OHLCV ----

    def _ohlcv(self, symbol, timeframe, since, limit):
        """OHLCV in ccxt order, resampling when the timeframe is not native.

        For derived timeframes we must over-fetch the source interval (ratio x
        the requested count) and aggregate; `candles_range` pages past the
        1000-row server cap so a 4h request for 1000 candles still resolves.
        """
        pair = self.market(symbol)["id"] if self.markets else symbol
        limit = int(limit or api.MAX_LIMIT)
        native = timeframe in api.INTERVAL_MS
        src = timeframe if native else DERIVED_TF.get(timeframe)
        if not src:
            raise ccxt.BadRequest(f"coindcx: unsupported timeframe {timeframe}")

        span = TF_MS[timeframe] * limit
        if since is None:
            # Tail request: one page is enough for native, paged for derived.
            rows = (api.candles(pair, src, limit=limit) if native
                    else api.candles_range(pair, src, self.milliseconds() - span))
        else:
            rows = api.candles_range(pair, src, int(since), int(since) + span)
        if not native:
            rows = api.resample(rows, src, timeframe)
        return rows[:limit] if since is not None else rows[-limit:]

    # ---- TICKERS ----

    def _ticker_from_rt(self, symbol, rt):
        """Build a ccxt ticker from the realtime futures snapshot row."""
        last = float(rt.get("ls") or 0)
        return {
            "symbol": symbol, "timestamp": None, "datetime": None,
            "high": float(rt.get("h") or 0) or None,
            "low": float(rt.get("l") or 0) or None,
            "bid": None, "ask": None,  # snapshot carries no top-of-book
            "last": last, "close": last,
            "previousClose": None,
            "percentage": float(rt.get("pc") or 0),
            # `v` is QUOTE volume despite the bare name — B-BTC_USDT reports
            # ~8.5e9, which is only sensible as USD. baseVolume is the derived
            # one, hence tickers_have_quoteVolume stays honest at the ft layer.
            "quoteVolume": float(rt.get("v") or 0),
            "baseVolume": (float(rt.get("v") or 0) / last) if last else None,
            "info": rt,
        }

    def _tickers(self, symbols=None):
        rt = api.futures_realtime()
        out = {}
        for pair, row in rt.items():
            symbol = api.dcx_to_ft(pair)
            if symbol and (symbols is None or symbol in symbols):
                out[symbol] = self._ticker_from_rt(symbol, row)
        return out

    def _funding_rates(self, symbols=None):
        """Realtime funding only — `fr` is the current 8h rate as a fraction."""
        rt = api.futures_realtime()
        out = {}
        for pair, row in rt.items():
            symbol = api.dcx_to_ft(pair)
            if symbol and (symbols is None or symbol in symbols):
                out[symbol] = {
                    "symbol": symbol, "fundingRate": float(row.get("fr") or 0),
                    "nextFundingRate": float(row.get("efr") or 0),
                    "markPrice": float(row.get("mp") or 0),
                    "timestamp": None, "datetime": None, "info": row,
                }
        return out

    def _order_book(self, symbol, limit):
        pair = self.market(symbol)["id"] if self.markets else symbol
        ob = api.orderbook(pair, limit or 50)
        return {"symbol": symbol, "bids": ob["bids"], "asks": ob["asks"],
                "timestamp": ob["timestamp"], "datetime": None, "nonce": None}

    def _balance(self):
        wallets = api.futures_wallets(self.apiKey, self.secret)
        out = {"info": wallets, "free": {}, "used": {}, "total": {}}
        for w in wallets:
            cur = w.get("currency_short_name")
            if not cur:
                continue
            free = float(w.get("balance") or 0)
            used = float(w.get("locked_balance") or 0)
            out["free"][cur] = free
            out["used"][cur] = used
            out["total"][cur] = free + used
            out[cur] = {"free": free, "used": used, "total": free + used}
        return out

    def _create_order(self, symbol, type_, side, amount, price, params):
        pair = self.market(symbol)["id"]
        order_type = "market_order" if type_ == "market" else "limit_order"
        res = api.create_futures_order(
            pair, side, amount, self.apiKey, self.secret, price=price,
            leverage=params.get("leverage", 1), order_type=order_type,
        )
        if not res:
            raise ccxt.ExchangeError(f"coindcx: order rejected for {symbol}")
        return self._parse_order(res if isinstance(res, dict) else res[0], symbol)

    def _parse_order(self, o, symbol=None):
        status = {"open": "open", "filled": "closed", "cancelled": "canceled",
                  "partially_filled": "open"}.get(o.get("status"), o.get("status"))
        filled = float(o.get("total_quantity", 0) or 0) - float(o.get("remaining_quantity", 0) or 0)
        return {
            "id": str(o.get("id") or ""), "clientOrderId": o.get("client_order_id"),
            "symbol": symbol or api.dcx_to_ft(o.get("pair", "")),
            "type": "market" if o.get("order_type") == "market_order" else "limit",
            "side": o.get("side"), "price": float(o.get("price") or 0) or None,
            "average": float(o.get("avg_price") or 0) or None,
            "amount": float(o.get("total_quantity") or 0),
            "filled": filled,
            "remaining": float(o.get("remaining_quantity") or 0),
            "status": status, "timestamp": o.get("created_at"),
            "datetime": None, "fee": None, "trades": [], "info": o,
        }


class coindcx(_CoindcxCore, ccxt.Exchange):  # noqa: N801 — ccxt names are lowercase
    """Synchronous ccxt-compatible CoinDCX client."""

    def fetch_markets(self, params={}):
        return self._build_markets()

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None, params={}):
        return self._ohlcv(symbol, timeframe, since, limit)

    def fetch_ticker(self, symbol, params={}):
        t = self._tickers([symbol]).get(symbol)
        if t is None:
            raise ccxt.BadSymbol(f"coindcx: no ticker for {symbol}")
        return t

    def fetch_tickers(self, symbols=None, params={}):
        return self._tickers(symbols)

    def fetch_funding_rates(self, symbols=None, params={}):
        return self._funding_rates(symbols)

    def fetch_funding_rate(self, symbol, params={}):
        return self._funding_rates([symbol]).get(symbol, {})

    def fetch_order_book(self, symbol, limit=None, params={}):
        return self._order_book(symbol, limit)

    def fetch_l2_order_book(self, symbol, limit=None, params={}):
        return self._order_book(symbol, limit)

    def fetch_balance(self, params={}):
        return self._balance()

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        return self._create_order(symbol, type, side, amount, price, params)

    def cancel_order(self, id, symbol=None, params={}):
        api.cancel_futures_order(id, self.apiKey, self.secret)
        return {"id": id, "symbol": symbol, "status": "canceled", "info": {}}

    def fetch_order(self, id, symbol=None, params={}):
        o = api.fetch_futures_order(id, self.apiKey, self.secret)
        if not o:
            raise ccxt.OrderNotFound(f"coindcx: order {id} not found")
        return self._parse_order(o, symbol)

    def fetch_positions(self, symbols=None, params={}):
        return api.futures_positions(self.apiKey, self.secret)

    def set_leverage(self, leverage, symbol=None, params={}):
        # CoinDCX takes leverage per-order, not as account state; freqtrade only
        # needs this call to not fail, and the value is applied in _create_order.
        return {"leverage": leverage, "symbol": symbol}

    def set_margin_mode(self, marginMode, symbol=None, params={}):
        # Isolated is the only mode INR-M futures offer, so anything else is a
        # config error worth surfacing loudly rather than silently ignoring.
        if marginMode and str(marginMode).lower() not in ("isolated", ""):
            raise ccxt.NotSupported("coindcx: only isolated margin is supported")
        return {"marginMode": "isolated", "symbol": symbol}


class _CoindcxAsync(_CoindcxCore, ccxt_async.Exchange):
    """Async ccxt client — delegates to the blocking core in a worker thread.

    Reimplementing every endpoint against aiohttp would double the number of
    places CoinDCX's quirks are encoded. Freqtrade issues a handful of OHLCV
    calls per cycle for 3 pairs, so a thread hop is far cheaper than that risk.
    """

    async def _run(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    async def fetch_markets(self, params={}):
        return await self._run(self._build_markets)

    async def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=None, params={}):
        return await self._run(self._ohlcv, symbol, timeframe, since, limit)

    async def fetch_ticker(self, symbol, params={}):
        t = (await self._run(self._tickers, [symbol])).get(symbol)
        if t is None:
            raise ccxt.BadSymbol(f"coindcx: no ticker for {symbol}")
        return t

    async def fetch_tickers(self, symbols=None, params={}):
        return await self._run(self._tickers, symbols)

    async def fetch_funding_rates(self, symbols=None, params={}):
        return await self._run(self._funding_rates, symbols)

    async def fetch_funding_rate(self, symbol, params={}):
        return (await self._run(self._funding_rates, [symbol])).get(symbol, {})

    async def fetch_order_book(self, symbol, limit=None, params={}):
        return await self._run(self._order_book, symbol, limit)

    async def fetch_l2_order_book(self, symbol, limit=None, params={}):
        return await self._run(self._order_book, symbol, limit)

    async def fetch_balance(self, params={}):
        return await self._run(self._balance)

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        return await self._run(self._create_order, symbol, type, side, amount, price, params)

    async def cancel_order(self, id, symbol=None, params={}):
        await self._run(api.cancel_futures_order, id, self.apiKey, self.secret)
        return {"id": id, "symbol": symbol, "status": "canceled", "info": {}}

    async def fetch_order(self, id, symbol=None, params={}):
        o = await self._run(api.fetch_futures_order, id, self.apiKey, self.secret)
        if not o:
            raise ccxt.OrderNotFound(f"coindcx: order {id} not found")
        return self._parse_order(o, symbol)

    async def fetch_positions(self, symbols=None, params={}):
        return await self._run(api.futures_positions, self.apiKey, self.secret)

    async def set_leverage(self, leverage, symbol=None, params={}):
        return {"leverage": leverage, "symbol": symbol}

    async def set_margin_mode(self, marginMode, symbol=None, params={}):
        if marginMode and str(marginMode).lower() not in ("isolated", ""):
            raise ccxt.NotSupported("coindcx: only isolated margin is supported")
        return {"marginMode": "isolated", "symbol": symbol}


def register_ccxt():
    """Make `coindcx` discoverable to freqtrade's `_init_ccxt`. Idempotent.

    freqtrade gates on `name in <module>.exchanges` and then does
    `getattr(<module>, name)`, so both the list and the attribute must exist.
    ccxt.pro is tried first for the async client and falls back to
    async_support; we bind the same async class on whichever modules exist so
    either path resolves. The class advertises watchOHLCV=False, so freqtrade
    will not attempt a websocket against it regardless of which one it picks.
    """
    for mod, cls in ((ccxt, coindcx), (ccxt_async, _CoindcxAsync),
                     (getattr(ccxt, "pro", None), _CoindcxAsync)):
        if mod is None:
            continue
        setattr(mod, "coindcx", cls)
        names = getattr(mod, "exchanges", None)
        if isinstance(names, list) and "coindcx" not in names:
            names.append("coindcx")


class Coindcx(Exchange):
    """CoinDCX — INR-margined perpetuals (no TDS, slab-rate tax)."""

    _ft_has: FtHas = {
        # 1000, not the 2000 originally specced: the server 422s above 1000
        # ("limit must be less than or equal to 1000"). Paging happens inside
        # coindcx_api.candles_range, so history depth is unaffected.
        "ohlcv_candle_limit": 1000,
        "ohlcv_has_history": True,
        "trades_has_history": False,
        "stoploss_on_exchange": False,
        "ws_enabled": False,
        # Depths 5 and 100 return an empty book with HTTP 200, so only offer
        # the three that actually populate.
        "l2_limit_range": [10, 20, 50],
    }
    _ft_has_futures: FtHas = {
        "tickers_have_quoteVolume": False,  # derived from base volume, not reported
        "funding_fee_timeframe": "8h",  # instrument.funding_frequency == 8
        "needs_trading_fees": False,
        "uses_leverage_tiers": False,
        "mark_ohlcv_price": "mark",
        "mark_ohlcv_timeframe": "1h",
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]


register_ccxt()
fx.Coindcx = Coindcx
