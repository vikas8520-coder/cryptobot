# ScalpV3_1h — VWAP reversion on 1h timeframe where fees are a smaller fraction.
#
# The 5m edge (~0.04%/trade) is far below the 0.2% round-trip fee. On 1h,
# VWAP deviations are 1-3% wide, so the edge per trade should be much larger
# relative to fees. Fewer trades = less fee drag.

import numpy as np
from freqtrade.strategy import (IStrategy, merge_informative_pair,
                                IntParameter, DecimalParameter)
from pandas import DataFrame
import talib.abstract as ta


class ScalpV3_1h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    # On 1h, give the reversion more time to play out (24h max via ROI)
    minimal_roi = {"0": 0.03, "720": 0.01, "1440": 0.0}
    stoploss = -0.05
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 500

    band = DecimalParameter(1.0, 3.0, default=2.0, decimals=1, space="buy", optimize=False)
    adx_max = IntParameter(15, 40, default=25, space="buy", optimize=False)
    vol_mult = DecimalParameter(0.8, 2.0, default=1.0, decimals=1, space="buy", optimize=False)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")

        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_vol = df.groupby(day)["volume"].cumsum()
        cum_tpv = (tp * df["volume"]).groupby(day).cumsum()
        df["vwap"] = cum_tpv / cum_vol.replace(0, np.nan)
        dev = df["close"] - df["vwap"]
        df["vwap_sd"] = dev.groupby(day).transform(
            lambda s: s.expanding(min_periods=12).std())

        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_tf).copy()
            inf["ema50"] = ta.EMA(inf, timeperiod=50)
            inf["ema100"] = ta.EMA(inf, timeperiod=100)
            df = merge_informative_pair(
                df, inf[["date", "close", "ema50", "ema100"]],
                self.timeframe, self.inf_tf, ffill=True)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        up_4h = (dataframe[f"close_{s}"] > dataframe[f"ema100_{s}"]) & \
                (dataframe[f"ema50_{s}"] > dataframe[f"ema100_{s}"])
        lower = dataframe["vwap"] - self.band.value * dataframe["vwap_sd"]
        dataframe.loc[
            (
                (dataframe["close"] < lower)
                & (dataframe["adx"] < self.adx_max.value)
                & up_4h
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] >= dataframe["vwap"]) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
