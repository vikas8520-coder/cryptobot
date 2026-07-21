# TrendFollow — a TREND-FOLLOWING strategy, the opposite philosophy to the
# RsiMaCross family (which were mean-reversion: buy dips, sell rips).
#
# The RsiMaCross bots missed the +68% bull run because they capped every win
# at ~4% and sold the moment a coin got strong. TrendFollow does the reverse:
#
#   BUY STRENGTH, not weakness:
#     - full bullish EMA stack: close > EMA20 > EMA50 > EMA100
#     - ADX > 20  (the trend is actually strong, not chop)
#     This enters when a real uptrend is in force and stays in while it holds.
#
#   LET WINNERS RUN:
#     - minimal_roi effectively DISABLED (set absurdly high) so profit is never
#       auto-capped — a 50% winner is allowed to become a 100% winner.
#     - a TRAILING STOP does the profit-taking: once a trade is up 10%, it
#       trails 5% below the peak, riding the move as far as the trend goes.
#
#   CUT LOSERS / EXIT ON TREND BREAK:
#     - hard stop -10% (trends need room to breathe; tight stops get shaken out)
#     - exit when the stack breaks down: EMA20 < EMA50, or close falls below EMA50
#
# Expected trade-off: should finally CAPTURE bull runs, but will get chopped up
# in sideways/ranging markets (whipsaw). That regime trade-off is the whole point.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class TrendFollow(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    # ROI disabled: never auto-cap a winner. 10 = 1000%, so it never triggers;
    # the trailing stop and trend-break exit decide when to leave.
    minimal_roi = {"0": 10}

    # Wide stop — a trend needs room. Tight stops die to normal pullbacks.
    stoploss = -0.10

    # The engine of "let winners run": once up 10%, trail 5% below the peak.
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 120  # for EMA100

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # full bullish EMA stack — price leading, trend aligned up
                (dataframe["close"] > dataframe["ema_fast"])
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["ema_slow"] > dataframe["ema_trend"])
                # trend is strong, not chop
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # trend broke down: momentum rolled over or price lost the mid MA
                (
                    (dataframe["ema_fast"] < dataframe["ema_slow"])
                    | (dataframe["close"] < dataframe["ema_slow"])
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
