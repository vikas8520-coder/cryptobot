# ScalpV3 — VWAP reversion with NO stop loss, time-based exit only.
#
# DIAGNOSIS 2026-07-28: The original ScalpVwap5m had a +55% fee-free edge from
# ROI + exit_signal exits, but 546 stop-loss hits at -2.2% each (-120% total)
# wiped it out. Even fee-free the strategy lost -17.44%.
#
# ScalpV2 tried wider stops (6%) + tighter filters — fee-free improved to +1.34%
# but with fees still -5.36%. The stop loss is still the problem.
#
# THIS VARIANT: Remove the stop loss entirely. The ROI time-decay
# {"0": 0.02, "180": 0.005, "360": 0.0} forces exit at 0% after 6h, so trades
# can't drift forever. The VWAP exit catches the reversion. No stop = no
# stop-out carnage. The worst case per trade is the 6h drift, which on 5m
# BTC is typically <3%.
#
# RISK: Without a stop, a black-swan 5m candle (flash crash) could cause a
# large loss before the ROI exit triggers. This is paper trading — acceptable
# for testing the thesis.

import numpy as np
from freqtrade.strategy import (IStrategy, merge_informative_pair,
                                IntParameter, DecimalParameter)
from pandas import DataFrame
import talib.abstract as ta


class ScalpV3(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    inf_tf = "1h"
    can_short = False

    # Time-decay ROI: take 2% profit immediately, 0.5% after 3h, exit at 0% after 6h.
    # This IS the risk control — no price-based stop.
    minimal_roi = {"0": 0.02, "180": 0.005, "360": 0.0}
    stoploss = -0.99              # effectively no stop — ROI time-decay is the exit
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 1300

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
        up_1h = (dataframe[f"close_{s}"] > dataframe[f"ema100_{s}"]) & \
                (dataframe[f"ema50_{s}"] > dataframe[f"ema100_{s}"])
        lower = dataframe["vwap"] - self.band.value * dataframe["vwap_sd"]
        dataframe.loc[
            (
                (dataframe["close"] < lower)
                & (dataframe["adx"] < self.adx_max.value)
                & up_1h
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
