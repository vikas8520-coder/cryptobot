# TrendFollowLS2 — long+short, but SHORTS are gated by a market-regime filter.
#
# TrendFollowLS problem: it shorted during bull-market PULLBACKS and got
# squeezed, cutting the bull return (+5.2% vs long-only +7.8%). The shorts
# themselves are fine in a bear; the issue is shorting inside an uptrend.
#
# FIX (minimal, surgical): only allow SHORT entries when BTC — the market tide —
# is in a CONFIRMED downtrend. Longs are left completely unchanged.
#   short allowed  <=>  BTC close < BTC EMA200  AND  BTC EMA50 < BTC EMA200,
#                       sustained for 12h (confirmation, so a brief BTC dip
#                       inside a bull doesn't switch shorting on).
# In a bull, BTC is above its EMA200 -> shorting is DISABLED -> no pullback
# shorts -> the bull return should recover to ~long-only. In a bear, BTC is
# below -> shorting is ON -> capture the downside.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowLS2(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"    # slowed from 1h (2026-07-20) to cut over-trading — fewer, higher-conviction decisions

    minimal_roi = {"0": 10}
    stoploss = -0.10
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220   # BTC EMA200 + 12-candle confirmation

    SHORT_CONFIRM = 12           # hours BTC must stay bearish before shorts turn on

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)
            bear_raw = (
                (btc["close"] < btc["btc_ema200"])
                & (btc["btc_ema50"] < btc["btc_ema200"])
            ).astype(int)
            # confirmed bear = last SHORT_CONFIRM candles ALL bear
            btc["btc_short_ok"] = bear_raw.rolling(self.SHORT_CONFIRM).min().fillna(0).astype(int)
            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_short_ok"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            dataframe["btc_short_ok_1h"] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # LONG — unchanged
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["ema_fast"])
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["ema_slow"] > dataframe["ema_trend"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # SHORT — bearish stack AND market confirmed in a downtrend
        dataframe.loc[
            (
                (dataframe["btc_short_ok_1h"] == 1)
                & (dataframe["close"] < dataframe["ema_fast"])
                & (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["ema_slow"] < dataframe["ema_trend"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                ((dataframe["ema_fast"] < dataframe["ema_slow"])
                 | (dataframe["close"] < dataframe["ema_slow"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        dataframe.loc[
            (
                ((dataframe["ema_fast"] > dataframe["ema_slow"])
                 | (dataframe["close"] > dataframe["ema_slow"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
