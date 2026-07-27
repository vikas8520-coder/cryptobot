# BrakedHoldDbg — hypothesis probe: does explicitly zero-initializing the
# enter_long/exit_long columns change the entry count vs BrakedHold?
# (Isolates the "uninitialized-column" theory. Everything else identical.)
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class BrakedHoldDbg(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.99
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 220

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe.loc[dataframe["close"] > dataframe["sma200"], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe.loc[dataframe["close"] < dataframe["sma200"], "exit_long"] = 1
        return dataframe
