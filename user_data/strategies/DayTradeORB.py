# DayTradeORB — research-backed replacement for the naive EMA-cross 15m day-trader.
#
# WHY THIS DESIGN (from the 2026-07-20 deep-research sweep):
#   - The ONLY intraday family that survives fees is BREAKOUT / trend-continuation,
#     because its edge is ASYMMETRIC PAYOFF: winners are trend-sized (>1%), so even a
#     40-50% hit rate nets positive after the ~0.2% round-trip toll. Mean-reversion
#     wins often but small, and fees eat the small wins.
#   - Opening-Range Breakout (ORB) was the #1-ranked approach. Crypto has no single
#     "open", so we use the de-facto convention: the 00:00 UTC candle defines the day's
#     opening range; we trade a CLOSE above that range's high.
#   - 1h timeframe (not 15m): community + research agree 1h gives cleaner crypto signals
#     and fewer fee-losing fakeouts.
#   - Trend gate: only take breakouts while price is above the 1h EMA200 (don't buy a
#     breakout inside a downtrend).
#
# THE "NO OVERNIGHT" RULE that makes this day-trading (not swing): custom_exit force-
# flattens any open position at/after 23:00 UTC, before the next session's OR forms.
#
# Spot config => LONG ONLY (shorts would need the futures config). No lookahead: the
# opening-range high/low come from the 00:00 candle, which is fully closed before any
# hour>0 candle can trigger an entry; OR levels are forward-filled within the day only.

from datetime import datetime

from freqtrade.strategy import IStrategy, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class DayTradeORB(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}         # never cap a breakout winner; trailing does exits
    stoploss = -0.03                 # backstop below the breakout

    # Let winners run, then lock gains: once +2%, trail 1% under the peak.
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220       # EMA200 on 1h

    SESSION_CLOSE_HOUR = 23          # flatten before the next 00:00 UTC session

    # Hyperoptable: require the breakout to clear OR-high by this % buffer (0 = default).
    # roi / stoploss / trailing are optimized via their hyperopt spaces (no code needed).
    buy_buffer = DecimalParameter(0.0, 0.6, default=0.0, decimals=2, space="buy",
                                  optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")
        is_or = df["date"].dt.hour == 0            # the 00:00 UTC candle = opening range

        # OR high/low are set on the 00:00 candle, then forward-filled through the day.
        # Only hour>0 candles act on them, so the OR candle is always fully closed first.
        df["or_high"] = df["high"].where(is_or).groupby(day).ffill()
        df["or_low"] = df["low"].where(is_or).groupby(day).ffill()

        df["ema_trend"] = ta.EMA(df, timeperiod=200)
        df["atr"] = ta.ATR(df, timeperiod=14)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        hour = dataframe["date"].dt.hour
        dataframe.loc[
            (
                (hour > 0)                                       # after the OR candle
                & dataframe["or_high"].notna()
                & (dataframe["close"] > dataframe["or_high"] * (1 + self.buy_buffer.value / 100.0))
                                                                 # breakout of the range (+buffer)
                & (dataframe["close"] > dataframe["ema_trend"])  # only in an uptrend
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Winners exit via the trailing stop; losers via the stop or the session flush.
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """No-overnight rule: force-flat at/after the session close hour (UTC)."""
        if current_time.hour >= self.SESSION_CLOSE_HOUR:
            return "session_close"
        return None
