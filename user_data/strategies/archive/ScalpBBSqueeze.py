# ScalpBBSqueeze — Bollinger Band squeeze breakout on 15m.
#
# THESIS 2026-07-28: Low volatility precedes explosive moves. When BB width
# narrows (squeeze), price is consolidating. When it breaks out with volume,
# a new trend is starting. Enter the breakout, ride it, exit when momentum fades.
#
# DESIGN:
#   - 15m BB(20, 2σ): identify squeeze when BB width < 50% of its 100-bar avg
#   - Entry: price breaks above upper BB after a squeeze, with volume
#   - Exit: price closes below middle BB (EMA20) — trend is done
#   - Stop: 3% (15m volatility)
#   - ROI: 3% immediate, 1% after 4h, 0% after 12h
#
# FEE AWARENESS: Breakout moves on 15m can be 1-3%, well above 0.2% fees.
# Fewer trades than 5m reversion = less fee drag.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class ScalpBBSqueeze(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = False

    minimal_roi = {"0": 0.03, "240": 0.01, "720": 0.0}
    stoploss = -0.03
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 200

    # BB params
    BB_PERIOD = 20
    BB_STD = 2.0
    SQUEEZE_RATIO = 0.5  # BB width < 50% of 100-bar avg = squeeze

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
        # Squeeze in the recent past (within last 10 candles)
        df["recent_squeeze"] = df["squeeze"].rolling(10).max()
        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["ema50"] = ta.EMA(df, timeperiod=50)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Breakout: price closes above upper BB after a recent squeeze, with volume
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 1.5 * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit when price closes below middle BB — trend is done
        dataframe.loc[
            ((dataframe["close"] < dataframe["bb_middle"]) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
