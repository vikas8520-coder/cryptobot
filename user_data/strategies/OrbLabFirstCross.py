# OrbLabFirstCross — LAB ONLY (not deployed). Isolates defect #1 found in the
# 2026-07-24 DayTradeORB backtest: the live strategy has NO first-cross condition.
#
# DayTradeORB enters on `close > or_high` on EVERY hour>0 candle, and or_high is
# forward-filled all day -> once price is above the range it re-buys hour after hour,
# chasing an already-extended move. That is a "price is above OR-high" STATE filter,
# not a breakout EVENT. Result: 2695 trades, entries scattered at random points in the
# day's range, many at the day's high right before the pullback -> 1655 stop_loss exits.
#
# This variant changes ONE thing: take only the day's FIRST close above the OR high.
# Everything else (stop, trailing, session close) is identical to the live strategy, so
# any delta is attributable to the entry defect alone.

from datetime import datetime

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class OrbLabFirstCross(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.03

    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220

    SESSION_CLOSE_HOUR = 23

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")
        is_or = df["date"].dt.hour == 0

        df["or_high"] = df["high"].where(is_or).groupby(day).ffill()
        df["or_low"] = df["low"].where(is_or).groupby(day).ffill()
        df["ema_trend"] = ta.EMA(df, timeperiod=200)
        df["atr"] = ta.ATR(df, timeperiod=14)

        # THE FIX: mark only the first candle of each day that closes above OR-high.
        above = (df["close"] > df["or_high"]) & (df["date"].dt.hour > 0)
        df["breakout_seq"] = above.groupby(day).cumsum()
        df["first_break"] = above & (df["breakout_seq"] == 1)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                dataframe["first_break"]
                & dataframe["or_high"].notna()
                & (dataframe["close"] > dataframe["ema_trend"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if current_time.hour >= self.SESSION_CLOSE_HOUR:
            return "session_close"
        return None
