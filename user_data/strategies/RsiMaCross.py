# RsiMaCross — a simple, transparent starter strategy.
#
# ENTRY (buy) when BOTH are true:
#   - RSI(14) is below 30  (asset is "oversold")
#   - fast MA (9) crosses ABOVE slow MA (21)  (momentum turning up)
#
# EXIT (sell) when EITHER is true:
#   - RSI(14) rises above 70  (asset is "overbought")
#   - fast MA (9) crosses BELOW slow MA (21)  (momentum turning down)
#
# Plus a hard stop-loss and a ROI table (take-profit ladder) as safety nets.
# This is a teaching strategy, not a money printer — the point is to learn
# the framework and see real backtest numbers. Tune it before ever going live.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class RsiMaCross(IStrategy):
    # Freqtrade strategy interface version
    INTERFACE_VERSION = 3

    # Candle size this strategy operates on
    timeframe = "5m"

    # --- Risk / exit safety nets ---------------------------------------

    # Take-profit ladder: sell when profit reaches these fractions after N minutes.
    # e.g. "0": 0.04 => take 4% profit immediately if available; decays over time.
    minimal_roi = {
        "0": 0.04,
        "30": 0.02,
        "60": 0.01,
        "120": 0
    }

    # Hard stop-loss: bail if a trade drops 5%. Your single most important line.
    stoploss = -0.05

    # Trailing stop: lock in gains once a trade moves in your favor.
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    # Only act on new, closed candles (cheaper, less noise).
    process_only_new_candles = True

    # Number of candles the strategy needs before it can produce a signal.
    startup_candle_count = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Momentum oscillator
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # Two moving averages for the crossover
        dataframe["ma_fast"] = ta.SMA(dataframe, timeperiod=9)
        dataframe["ma_slow"] = ta.SMA(dataframe, timeperiod=21)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 30)
                & (qtpylib.crossed_above(dataframe["ma_fast"], dataframe["ma_slow"]))
                & (dataframe["volume"] > 0)  # ignore dead candles
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (
                    (dataframe["rsi"] > 70)
                    | (qtpylib.crossed_below(dataframe["ma_fast"], dataframe["ma_slow"]))
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
