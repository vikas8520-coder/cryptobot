# Scalp1m — a LEARNING-LAB scalper on 1-minute candles.
#
# PURPOSE: this bot exists to *demonstrate why scalping loses*, not to make money.
# It trades the classic retail scalp — buy a fast oversold dip, sell the tiny bounce —
# and its config pays the FULL real-world cost of doing so (market orders that cross
# the spread + 0.1% fee each side). Watch the "fee drag" line in /stats: on 1-minute
# targets this small, the exchange takes most of every win before you're even right.
#
# STRATEGY (deliberately ordinary — this is the trade everyone tries):
#   ENTRY long:  RSI(14) < 30  (oversold)  AND  close > EMA200  (only dip-buy in an
#                uptrend, never catch a falling knife) AND volume present.
#   EXIT:        RSI(14) > 55  (the bounce is spent)  OR  minimal_roi (+0.4% grabbed
#                fast, decaying to 0)  OR  hard stop -0.5%.
#
# The math that kills it: target +0.4%, but a round trip costs ~0.2% in fees + the
# spread. Two-thirds of a winning scalp is gone to friction — and that's before the
# losers. In India, add 1% TDS on EVERY sell. This bot is the proof, running live.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class Scalp1m(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1m"

    # Take a tiny profit fast, then let the target decay so a stalled scalp is cut.
    minimal_roi = {"0": 0.004, "10": 0.002, "20": 0.0}

    # Tight stop — scalps don't get room; a scalp that's down 0.5% is wrong.
    stoploss = -0.005
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 200  # EMA200 trend filter

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 30)                      # oversold flush
                & (dataframe["close"] > dataframe["ema_trend"])  # only in an uptrend
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] > 55)                      # bounce spent
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
