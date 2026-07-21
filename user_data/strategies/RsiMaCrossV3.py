# RsiMaCrossV3 — V2's entries + a TREND FILTER to stop catching falling knives.
#
# The lesson from V2: dip-buying (RSI < 35) in a falling market is a meat
# grinder — you buy, price keeps dropping, you stop out, 1,800 times over.
# V3 adds one gate: only take entries when price is ABOVE its 50-period MA,
# i.e. only trade WITH the broader uptrend, never against a downtrend.
#
# ENTRY (buy) when the TREND FILTER passes AND either signal fires:
#   TREND FILTER:  close > MA(50)        (only buy in an uptrend)
#   AND (
#       RSI(14) < 35                                  (oversold dip), OR
#       fast MA (9) crosses ABOVE slow MA (21)
#         AND RSI(14) < 55                            (momentum turning up)
#   )
#
# EXIT (sell) when EITHER is true:
#   - RSI(14) > 65                                    (overbought), OR
#   - fast MA (9) crosses BELOW slow MA (21)          (momentum turning down)
#
# Same stop-loss / ROI / trailing safety nets as before.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class RsiMaCrossV3(IStrategy):
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
    # Need enough history for the 50-period trend MA.
    startup_candle_count = 60

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ma_fast"] = ta.SMA(dataframe, timeperiod=9)
        dataframe["ma_slow"] = ta.SMA(dataframe, timeperiod=21)
        # Trend filter: the longer MA that tells us "up" vs "down".
        dataframe["ma_trend"] = ta.SMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # --- trend filter: only trade with the uptrend ---
                (dataframe["close"] > dataframe["ma_trend"])
                # --- entry signal (same as V2) ---
                & (
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
