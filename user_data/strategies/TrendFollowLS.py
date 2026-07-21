# TrendFollowLS — TrendFollow made LONG *and* SHORT (structural change).
#
# Every strategy so far was long-only (spot): it can only profit when price
# RISES, so in a bear market the best it can do is sit in cash. This version
# runs on FUTURES with can_short=True, so it can also SELL SHORT — open a
# position that PROFITS when price FALLS. Now a downtrend is an opportunity,
# not just something to hide from.
#
# The logic is a mirror:
#   LONG  when the trend is up:   close > EMA20 > EMA50 > EMA100, ADX strong
#   SHORT when the trend is down: close < EMA20 < EMA50 < EMA100, ADX strong
#   exit each side when its trend breaks; let winners run via the trailing stop
#   (trailing works on profit in EITHER direction).
#
# Requires: trading_mode=futures, margin_mode=isolated (see config_futures.json).
# Leverage kept at 1x to isolate the effect of SHORTING itself, not leverage.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowLS(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True          # <-- the structural switch: allow short positions

    timeframe = "1h"

    minimal_roi = {"0": 10}   # never auto-cap a winner
    stoploss = -0.10
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 120

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0  # 1x — no leverage; we're testing shorting, not leverage risk

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # LONG: full bullish stack
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["ema_fast"])
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["ema_slow"] > dataframe["ema_trend"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # SHORT: full bearish stack (mirror image)
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["ema_fast"])
                & (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["ema_slow"] < dataframe["ema_trend"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # exit LONG when uptrend breaks
        dataframe.loc[
            (
                ((dataframe["ema_fast"] < dataframe["ema_slow"])
                 | (dataframe["close"] < dataframe["ema_slow"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1

        # exit SHORT when downtrend breaks (price/momentum turns back up)
        dataframe.loc[
            (
                ((dataframe["ema_fast"] > dataframe["ema_slow"])
                 | (dataframe["close"] > dataframe["ema_slow"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
