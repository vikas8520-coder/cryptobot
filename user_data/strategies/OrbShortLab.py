# OrbShortLab — LAB ONLY, not for deployment. Tests the one ORB variant that showed
# any measurable edge in the 2026-07-28 DayTradeORB post-mortem.
#
# The long-side ORB that port 8084 trades measured a forward return of ~0.00% across
# 49 coins x 4.5y at every range length and horizon. The SHORT side did not:
#   - break BELOW a TIGHT opening range (<1.5% wide) while below the 1h EMA200
#   - +0.198% mean 12h return vs +0.097% for the same regime without the breakout
#     (incremental t=3.62), and positive in every calendar year 2022-2026.
# This file exists to check whether that survives real fills, fees and funding, or
# whether it is an event-study artifact that dies on contact with a backtester.

from datetime import datetime, timedelta

from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
from pandas import DataFrame
import talib.abstract as ta


class OrbShortLab(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True

    minimal_roi = {"0": 0.05}
    stoploss = -0.03
    trailing_stop = False

    MAX_HOLD_HOURS = 24

    process_only_new_candles = True
    startup_candle_count = 220

    or_hours = IntParameter(2, 6, default=4, space="buy", optimize=True)
    tight_w = DecimalParameter(0.008, 0.025, default=0.015, decimals=3, space="buy",
                               optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")
        hour = df["date"].dt.hour
        orh = self.or_hours.value

        in_or = hour < orh
        agg = df[in_or].groupby(day[in_or]).agg(_hi=("high", "max"), _lo=("low", "min"))
        df["or_high"] = day.map(agg["_hi"])
        df["or_low"] = day.map(agg["_lo"])

        # A tight opening range is the filter that mattered — a coiled range breaking
        # down resolves; a range that was already 3% wide has spent its move.
        df["tight"] = (df["or_high"] - df["or_low"]) / df["close"] < self.tight_w.value
        df["ema_trend"] = ta.EMA(df, timeperiod=200)

        broke = (hour >= orh) & df["or_low"].notna() & (df["close"] < df["or_low"])
        df["first_break"] = broke & (broke.groupby(day).cumsum() == 1)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                dataframe["first_break"]
                & dataframe["tight"]
                & (dataframe["close"] < dataframe["ema_trend"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        if current_time - trade.open_date_utc >= timedelta(hours=self.MAX_HOLD_HOURS):
            return "max_hold"
        return None

    def leverage(self, pair: str, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0
