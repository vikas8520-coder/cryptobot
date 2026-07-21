# RsiMaCrossV2 — a LOOSENED version of RsiMaCross.
#
# The original required RSI < 30 AND an MA crossover at the same instant,
# which almost never happens (only 7 trades in 105 days). V2 relaxes that so
# the bot actually trades, while still keeping the logic honest.
#
# ENTRY (buy) when EITHER is true:
#   - RSI(14) < 35                                   (oversold dip-buy), OR
#   - fast MA (9) crosses ABOVE slow MA (21)
#     AND RSI(14) < 55                               (momentum turning up,
#                                                      but not already overbought)
#
# EXIT (sell) when EITHER is true:
#   - RSI(14) > 65                                   (overbought), OR
#   - fast MA (9) crosses BELOW slow MA (21)         (momentum turning down)
#
# Same stop-loss / ROI / trailing safety nets as V1.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class RsiMaCrossV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"

    minimal_roi = {
        "0": 0.04,
        "30": 0.02,
        "60": 0.01,
        "120": 0
    }

    stoploss = -0.05

    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ma_fast"] = ta.SMA(dataframe, timeperiod=9)
        dataframe["ma_slow"] = ta.SMA(dataframe, timeperiod=21)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (
                    (dataframe["rsi"] < 35)
                    | (
                        qtpylib.crossed_above(dataframe["ma_fast"], dataframe["ma_slow"])
                        & (dataframe["rsi"] < 55)
                    )
                )
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (
                    (dataframe["rsi"] > 65)
                    | (qtpylib.crossed_below(dataframe["ma_fast"], dataframe["ma_slow"]))
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
