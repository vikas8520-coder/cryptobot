# TrendFollowShortOnly — short-only futures strategy gated by BTC downtrend.
#
# Lesson from TrendFollowLS2: long+short trend-momentum is not robust over a
# full 6-year crypto cycle. This variant drops longs entirely and only shorts
# altcoin perpetuals when BTC is in a confirmed bear regime and the coin is
# already trading below its own 200-EMA. The goal is to capture the bulk of
# bear-market declines while being flat during bull markets.
#
# Entry rule:
#   short allowed <=> BTC close < BTC EMA200 AND BTC EMA50 < BTC EMA200,
#                     sustained for 12h,
#                 AND coin close < coin EMA200,
#                 AND ADX > 35,
#                 AND volume > 20-period volume MA.
#
# Exit rule:
#   close short <=> BTC is no longer in confirmed bear regime
#                OR coin close > coin EMA200.
# A hard stoploss at -8% and a trailing stop still protect against sharp
# bear-market squeezes.

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowShortOnly(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"

    minimal_roi = {"0": 10}
    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.03
    trailing_stop_positive_offset = 0.06
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220   # BTC EMA200 + confirmation candles
    ADX_MIN = 35

    # Wall-clock duration BTC must stay bearish before shorts turn on.
    CONFIRM_HOURS = 12

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        short_ok_col = f"btc_short_ok_{self.timeframe}"

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)

            bear_raw = (
                (btc["close"] < btc["btc_ema200"])
                & (btc["btc_ema50"] < btc["btc_ema200"])
            ).astype(int)

            confirm_candles = max(
                1, self.CONFIRM_HOURS * 60 // timeframe_to_minutes(self.timeframe)
            )
            btc["btc_short_ok"] = bear_raw.rolling(confirm_candles).min().fillna(0).astype(int)

            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_short_ok"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            dataframe[short_ok_col] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # SHORT when BTC is confirmed bearish and the coin is below its 200-EMA
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 1)
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["adx"] > self.ADX_MIN)
                & (dataframe["volume"] > dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit short when BTC is no longer bearish or the coin reclaims 200-EMA
        dataframe.loc[
            (
                ((dataframe[f"btc_short_ok_{self.timeframe}"] == 0)
                 | (dataframe["close"] > dataframe["ema200"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
