# SolScalp1hOpt — hyperoptable BB squeeze breakout on SOL/USDT:USDT perp, 1h.
#
# Same thesis as SolScalp1h but with IntParameter/DecimalParameter for hyperopt.
# Optimizes: BB period, BB std, squeeze ratio, ADX threshold, volume multiplier,
# stoploss, ROI levels.

from freqtrade.strategy import IStrategy, merge_informative_pair, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class SolScalp1hOpt(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    minimal_roi = {"0": 0.03, "360": 0.01, "720": 0.0}
    stoploss = -0.04
    trailing_stop = True
    trailing_stop_positive = 0.015
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True
    use_exit_signal = False

    process_only_new_candles = True
    startup_candle_count = 500

    # Hyperoptable params
    bb_period = IntParameter(15, 30, default=20, space="buy", optimize=True)
    bb_std = DecimalParameter(1.5, 2.5, default=2.0, decimals=1, space="buy", optimize=True)
    squeeze_ratio = DecimalParameter(0.3, 0.7, default=0.5, decimals=1, space="buy", optimize=True)
    adx_min = IntParameter(15, 40, default=25, space="buy", optimize=True)
    vol_mult = DecimalParameter(1.0, 3.0, default=2.0, decimals=1, space="buy", optimize=True)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        upper, middle, lower = ta.BBANDS(df["close"], timeperiod=self.bb_period.value,
                                          nbdevup=self.bb_std.value, nbdevdn=self.bb_std.value)
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        df["bb_width"] = (upper - lower) / middle
        df["bb_width_avg"] = df["bb_width"].rolling(100).mean()
        df["squeeze"] = (df["bb_width"] < self.squeeze_ratio.value * df["bb_width_avg"]).astype(int)
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
        prev_below = dataframe["close"].shift(1) < dataframe["bb_upper"].shift(1)
        strong_candle = (dataframe["close"] > dataframe["open"]) & \
                        ((dataframe["close"] - dataframe["open"]) > 0.5 * (dataframe["high"] - dataframe["low"]))
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & prev_below
                & strong_candle
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > self.adx_min.value)
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & up_4h
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe
