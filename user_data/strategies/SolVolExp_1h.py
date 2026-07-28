# SolVolExp_1h — Volatility expansion breakout on SOL perp, 1h.
#
# THESIS (from Claude Code's analysis): Direction isn't predictable on 1h SOL
# (lag-1 autocorr ~0), but volatility IS (autocorr +0.21-0.36, stable). So
# instead of predicting direction, we predict WHEN it will move and take
# the direction from the break itself.
#
# This uses ATR ratio (current ATR / avg ATR) to detect volatility compression,
# then enters when price breaks out of the compression range with a strong
# candle. No Bollinger Bands — pure ATR-based volatility detection.

from freqtrade.strategy import IStrategy, merge_informative_pair, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class SolVolExp_1h(IStrategy):
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

    atr_period = IntParameter(10, 20, default=14, space="buy", optimize=False)
    atr_ratio = DecimalParameter(0.5, 0.9, default=0.7, decimals=1, space="buy", optimize=False)
    adx_min = IntParameter(15, 35, default=20, space="buy", optimize=False)
    vol_mult = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="buy", optimize=False)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["atr"] = ta.ATR(df, timeperiod=self.atr_period.value)
        df["atr_avg"] = df["atr"].rolling(50).mean()
        df["atr_ratio"] = df["atr"] / df["atr_avg"]
        # Volatility compression: ATR is below threshold of its average
        df["compressed"] = (df["atr_ratio"] < self.atr_ratio.value).astype(int)
        df["recent_compress"] = df["compressed"].rolling(10).max()
        # Range high/low during compression
        df["range_high"] = df["high"].rolling(10).max().where(df["recent_compress"] == 1)
        df["range_low"] = df["low"].rolling(10).min().where(df["recent_compress"] == 1)

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
        # Breakout: close above recent range high after compression, strong candle
        strong_candle = (dataframe["close"] > dataframe["open"]) & \
                        ((dataframe["close"] - dataframe["open"]) > 0.5 * (dataframe["high"] - dataframe["low"]))
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["range_high"])
                & (dataframe["recent_compress"] == 1)
                & strong_candle
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
