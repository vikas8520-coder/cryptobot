# Prototype: minimal Freqtrade exchange subclass for Delta Exchange India.
# Injected into freqtrade.exchange so ExchangeResolver's getattr() finds it.
import freqtrade.exchange as fx
from freqtrade.enums import CandleType, MarginMode, TradingMode
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas


class Delta(Exchange):
    """Delta Exchange India — INR-margined perpetuals."""

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 2000,
        "ohlcv_has_history": True,
        "trades_has_history": False,
        "stoploss_on_exchange": False,
        "ws_enabled": False,
    }
    _ft_has_futures: FtHas = {
        "tickers_have_quoteVolume": False,
        "funding_fee_timeframe": "8h",
        "mark_ohlcv_price": "mark",
        "mark_ohlcv_timeframe": "1h",
        "needs_trading_fees": False,
        "uses_leverage_tiers": False,
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    async def _fetch_funding_rate_history(self, pair, timeframe, limit, since_ms=None):
        # ccxt has fetchFundingRateHistory=False for delta, but the v2/history/candles
        # endpoint serves funding rates under a "FUNDING:<market_id>" pseudo-symbol.
        # params overrides request in ccxt's extend(), so we can swap the symbol in.
        # Delta's candle endpoint has no 8h resolution, so pull 1h and keep only the
        # three daily funding stamps (00:00 / 08:00 / 16:00 UTC = 05:30/13:30/21:30 IST).
        market_id = self._api_async.market(pair)["id"]
        data = await self._api_async.fetch_ohlcv(
            pair, timeframe="1h", since=since_ms, limit=min(limit, 2000),
            params={"symbol": f"FUNDING:{market_id}"},
        )
        eight_h = 8 * 3600 * 1000
        # Delta quotes funding in percent; freqtrade expects a fraction.
        return [[c[0], c[4] / 100, 0, 0, 0, 0] for c in data if c[0] % eight_h == 0]


fx.Delta = Delta
