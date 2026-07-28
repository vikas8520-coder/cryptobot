# ScalpBBSqueezeV4 — BB squeeze breakout on 4h (swing strategy).
#
# EVOLUTION: V1(15m)→+11.8% fee-free/1903 trades. V2(15m tighter)→+3.9%/321.
# V3(1h)→+1.88%/169. Each step: fewer trades, edge per trade approaches fee.
# V4(4h): breakout moves are 5-10%, fees (0.2%) are negligible fraction.
# This is really a swing strategy now, not a scalp — but it uses the same
# BB squeeze breakout thesis that showed a real edge.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class ScalpBBSqueezeV4(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    inf_tf = "1d"
    can_short = False

    # On 4h, give breakouts lots of room: 8% immediate, 3% after 2 days, 0% after 5 days
    minimal_roi = {"0": 0.08, "2880": 0.03, "7200": 0.0}
    stoploss = -0.05
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 500  # audit 2026-07-28: was 250, 1d EMA200 unconverged by 0.17%

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
        up_1d = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 25)
                & (dataframe["volume"] > 2.0 * dataframe["vol_ma"])
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
