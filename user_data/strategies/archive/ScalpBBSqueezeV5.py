# ScalpBBSqueezeV5 — Loosened V4 filters for more trades.
#
# V4: +7.61% / PF 2.20 / DD 0.82% but only 54 trades (below 100 threshold).
# V5: squeeze ratio 0.6 (from 0.5), ADX > 20 (from 25), volume 1.5x (from 2.0x).
# Goal: ~100+ trades while keeping PF > 1.3.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class ScalpBBSqueezeV5(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    inf_tf = "1d"
    can_short = False

    minimal_roi = {"0": 0.08, "2880": 0.03, "7200": 0.0}
    stoploss = -0.05
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 250

    BB_PERIOD = 20
    BB_STD = 2.0
    SQUEEZE_RATIO = 0.6   # looser squeeze

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        upper, middle, lower = ta.BBANDS(df["close"], timeperiod=self.BB_PERIOD,
                                          nbdevup=self.BB_STD, nbdevdn=self.BB_STD)
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        df["bb_width"] = (upper - lower) / middle
        df["bb_width_avg"] = df["bb_width"].rolling(100).mean()
        df["squeeze"] = (df["bb_width"] < self.SQUEEZE_RATIO * df["bb_width_avg"]).astype(int)
        df["recent_squeeze"] = df["squeeze"].rolling(10).max()
        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["ema50"] = ta.EMA(df, timeperiod=50)

        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_tf).copy()
            inf["ema50"] = ta.EMA(inf, timeperiod=50)
            inf["ema200"] = ta.EMA(inf, timeperiod=200)
            df = merge_informative_pair(
                df, inf[["date", "close", "ema50", "ema200"]],
                self.timeframe, self.inf_tf, ffill=True)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        up_1d = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 20)           # looser
                & (dataframe["volume"] > 1.5 * dataframe["vol_ma"])  # looser
                & (dataframe["close"] > dataframe["ema50"])
                & up_1d
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["close"] < dataframe["bb_lower"]) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
