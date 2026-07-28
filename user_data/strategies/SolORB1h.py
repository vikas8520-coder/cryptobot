# SolORB1h — Opening Range Breakout on SOL/USDT:USDT perp, 1h.
#
# THESIS: The first 1-4 hours of each UTC day establish a range. When price
# breaks above that range with volume, a trend day is starting. Enter the
# breakout, ride it, exit at close or when the range is reclaimed.
#
# This is a classic intraday strategy adapted to 24/7 crypto. The "opening
# range" is defined as the first N hours after 00:00 UTC.

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class SolORB1h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    # ROI: 3% immediate, 1% after 6h, exit at 0% after 12h
    minimal_roi = {"0": 0.03, "360": 0.01, "720": 0.0}
    stoploss = -0.03
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True
    use_exit_signal = True

    process_only_new_candles = True
    startup_candle_count = 200

    # Hyperoptable: opening range length in hours
    orb_hours = IntParameter(2, 6, default=4, space="buy", optimize=False)
    vol_mult = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="buy", optimize=False)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")

        # Opening range: high/low of first N hours of each day
        df["hour"] = df["date"].dt.hour
        df["day"] = day

        # For each row, compute the OR high/low based on the first orb_hours of that day
        # We need to look back within the same day
        orb_h = self.orb_hours.value
        df["is_or_period"] = df["hour"] < orb_h

        # Compute cumulative high/low within the OR period for each day
        df["or_high"] = np.nan
        df["or_low"] = np.nan

        for d, group in df.groupby("day"):
            or_mask = group["is_or_period"]
            or_data = group[or_mask]
            if len(or_data) > 0:
                or_high = or_data["high"].max()
                or_low = or_data["low"].min()
                # Set OR high/low for all candles AFTER the OR period in this day
                after_or = group.index[~or_mask]
                df.loc[after_or, "or_high"] = or_high
                df.loc[after_or, "or_low"] = or_low

        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["ema50"] = ta.EMA(df, timeperiod=50)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Entry: price breaks above OR high with volume, after the OR period
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["or_high"])
                & (dataframe["or_high"].notna())
                & (~dataframe["is_or_period"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit: price breaks below OR low (range reclaimed = breakout failed)
        dataframe.loc[
            ((dataframe["close"] < dataframe["or_low"]) & (dataframe["or_low"].notna()) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
