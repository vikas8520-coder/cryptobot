# ScalpBBSqueezeV3 — BB squeeze breakout on 1h with trailing-style exit.
#
# V2 on 15m: fee-free +3.9% but -2.14% with fees (edge per trade 0.12% < 0.2% fee).
# V3 moves to 1h where breakout moves are 2-5%, giving more room above fees.
# Also: exit on lower BB instead of middle BB, letting winners run longer.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class ScalpBBSqueezeV3(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    # On 1h, give breakouts more room: 5% immediate, 2% after 12h, 0% after 24h
    minimal_roi = {"0": 0.05, "720": 0.02, "1440": 0.0}
    stoploss = -0.04
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 250

    BB_PERIOD = 20
    BB_STD = 2.0
    SQUEEZE_RATIO = 0.5

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
        up_4h = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 25)
                & (dataframe["volume"] > 2.0 * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & up_4h
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit on lower BB — lets winners run to the bottom of the band
        dataframe.loc[
            ((dataframe["close"] < dataframe["bb_lower"]) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
