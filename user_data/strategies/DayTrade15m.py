# DayTrade15m — a LEARNING-LAB day-trader on 15-minute candles.
#
# PURPOSE: sits between the scalper and the swing bots. A real day-trader takes a few
# momentum trades a day and — the defining rule — NEVER holds overnight. This bot
# enforces that discipline: any position still open after INTRADAY_MAX_HOURS is force-
# closed, so nothing carries risk into the next session. That's what makes it "day
# trading" and not "swing": it flattens to cash by end of day, every day.
#
# STRATEGY (intraday momentum):
#   ENTRY long:  EMA9 > EMA21 (fast trend up)  AND  RSI(14) > 50 (momentum with it)
#                AND  close > EMA50 (above the intraday mean)  AND volume present.
#   EXIT:        EMA9 < EMA21 (momentum rolled over)  OR  minimal_roi (+1.5%)  OR
#                stop -1.5%  OR  the intraday timeout (custom_exit below).
#
# It pays the same honest costs as the scalper (market orders + fee each side), just
# fewer times a day. The lesson here is subtler: even a *sensible* intraday momentum
# rule struggles to clear fees + the 1% India TDS-per-sell when it's forced to close
# daily instead of letting a real trend run for days like the swing bots do.

from datetime import datetime, timedelta, timezone

from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class DayTrade15m(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"

    minimal_roi = {"0": 0.015}     # +1.5% target — bigger than a scalp, smaller than a swing
    stoploss = -0.015              # symmetric -1.5% risk

    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 60      # EMA50

    INTRADAY_MAX_HOURS = 6         # the "no overnight" rule — flatten anything older

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema_mean"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["rsi"] > 50)
                & (dataframe["close"] > dataframe["ema_mean"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """The no-overnight rule: force-close any position older than the intraday cap.
        current_time is tz-aware UTC; trade.open_date_utc is too."""
        if current_time - trade.open_date_utc >= timedelta(hours=self.INTRADAY_MAX_HOURS):
            return "intraday_timeout"
        return None
