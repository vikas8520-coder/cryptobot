# RsiMaCrossV3Hopt — V3 with its thresholds exposed as HYPEROPT PARAMETERS.
#
# Same logic as RsiMaCrossV3 (trend filter + RSI/MA-cross entries), but the
# magic numbers are no longer hardcoded — they're IntParameters that Freqtrade's
# hyperopt (Optuna) will search over thousands of combinations to find the set
# that scored best on the historical data.
#
# We optimize:
#   - buy_rsi        : the "oversold" dip-buy threshold        (space: buy)
#   - buy_rsi_cross  : max RSI allowed on an MA-cross entry     (space: buy)
#   - sell_rsi       : the "overbought" exit threshold          (space: sell)
#   - the ROI table, stoploss, and trailing stop  (via --spaces on the CLI)
#
# IMPORTANT: hyperopt finds what fit the PAST best. That is NOT proof it works
# in the future — it's the #1 way beginners fool themselves (overfitting).
# Always re-validate tuned params on data the optimizer never saw.

from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class RsiMaCrossV3Hopt(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"

    # These are starting values; hyperopt overrides them while searching, and
    # writes the winners to a JSON that the strategy auto-loads afterward.
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
    startup_candle_count = 60

    # --- Tunable parameters (this is what makes it hyperopt-able) ----------
    buy_rsi = IntParameter(15, 45, default=35, space="buy", optimize=True)
    buy_rsi_cross = IntParameter(45, 65, default=55, space="buy", optimize=True)
    sell_rsi = IntParameter(55, 85, default=65, space="sell", optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["ma_fast"] = ta.SMA(dataframe, timeperiod=9)
        dataframe["ma_slow"] = ta.SMA(dataframe, timeperiod=21)
        dataframe["ma_trend"] = ta.SMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["ma_trend"])
                & (
                    (dataframe["rsi"] < self.buy_rsi.value)
                    | (
                        qtpylib.crossed_above(dataframe["ma_fast"], dataframe["ma_slow"])
                        & (dataframe["rsi"] < self.buy_rsi_cross.value)
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
                    (dataframe["rsi"] > self.sell_rsi.value)
                    | (qtpylib.crossed_below(dataframe["ma_fast"], dataframe["ma_slow"]))
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
