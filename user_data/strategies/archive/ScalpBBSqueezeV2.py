# ScalpBBSqueezeV2 — Tighter BB squeeze breakout to reduce trade count.
#
# V1 had +11.8% fee-free but 1903 trades — fees ate it (-24.5% with fees).
# This version: tighter squeeze (0.4 ratio), stronger volume (2x), higher ADX
# (25), and a 1h trend filter. Goal: fewer trades, same edge per trade.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class ScalpBBSqueezeV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    inf_tf = "1h"
    can_short = False

    minimal_roi = {"0": 0.03, "240": 0.01, "720": 0.0}
    stoploss = -0.03
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 250

    BB_PERIOD = 20
    BB_STD = 2.0
    SQUEEZE_RATIO = 0.4  # tighter squeeze

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
        up_1h = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 25)
                & (dataframe["volume"] > 2.0 * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & up_1h
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["close"] < dataframe["bb_middle"]) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
