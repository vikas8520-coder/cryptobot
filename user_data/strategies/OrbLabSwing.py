# OrbLabSwing — LAB ONLY (not deployed). Adds defect #3 fix: drops the no-overnight rule.
#
# Defect #3 from the 2026-07-24 backtest: session_close was the single largest trade
# bucket (3388 exits) and lost -580 USDT at -0.17% avg with a 31.6% win rate. Forcing
# flat at 23:00 UTC exits at a clock time that has nothing to do with the trade thesis:
# it cuts winners mid-trend and pays a round-trip fee to do it. Crypto trades 24/7 --
# there is no overnight gap risk to hedge against, so the rule imports an equities
# constraint that does not apply and charges fees for it.
#
# This variant keeps first-cross entry + the wider payoff geometry, and lets the trade
# live until the trail or the stop resolves it. This is no longer "day trading" -- it is
# the honest form of the breakout thesis: enter on the ORB event, hold the trend.

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class OrbLabSwing(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.06

    trailing_stop = True
    trailing_stop_positive = 0.04
    trailing_stop_positive_offset = 0.08
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")
        is_or = df["date"].dt.hour == 0

        df["or_high"] = df["high"].where(is_or).groupby(day).ffill()
        df["ema_trend"] = ta.EMA(df, timeperiod=200)

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
        # No session_close: the trail or the stop ends the trade, not the clock.
        return dataframe
