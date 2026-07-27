# OrbLabPayoff — LAB ONLY (not deployed). Adds defect #2 fix on top of first-cross.
#
# Defect #2 from the 2026-07-24 backtest: the payoff ratio is INVERTED relative to the
# strategy's own design brief. The docstring argues breakout works because "winners are
# trend-sized (>1%)", but trailing_stop_positive=0.01 / offset=0.02 locks a winner the
# moment it clears +2%, so realised winners averaged +1.77% while the -3% stop lost
# -3.00%. Losing 3 to win 1.77 needs a 63% hit rate; ORB does not have one. The trailing
# config truncates exactly the right tail the edge depends on.
#
# Fix: give the trade room. Stop widened to -6% (below typical 1h noise), and the trail
# only arms at +8%, then follows 4% behind the peak -> winners can run into the fat tail.
# Session close is still enforced, so this stays a day-trading strategy.

from datetime import datetime

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class OrbLabPayoff(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    minimal_roi = {"0": 100}
    stoploss = -0.06                 # was -0.03: 1h crypto noise stopped out good breakouts

    trailing_stop = True
    trailing_stop_positive = 0.04    # was 0.01
    trailing_stop_positive_offset = 0.08   # was 0.02 -> stop truncating winners at +2%
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220

    SESSION_CLOSE_HOUR = 23

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
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if current_time.hour >= self.SESSION_CLOSE_HOUR:
            return "session_close"
        return None
