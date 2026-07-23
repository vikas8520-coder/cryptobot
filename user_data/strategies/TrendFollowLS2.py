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

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowLS2(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"    # slowed from 1h (2026-07-20) to cut over-trading — fewer, higher-conviction decisions

    minimal_roi = {"0": 10}
    # audit 2026-07-23: live futures paper was 5W/41L PF 0.16. Nearly all force-exits
    # + signal-loss; cut hard-stop to 8% and arm trailing earlier so small wins lock.
    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.03
    trailing_stop_positive_offset = 0.06
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220   # BTC EMA200 + confirmation candles
    # Minimum ADX for entry (was hard-coded 20; too many weak-trend whipsaws).
    ADX_MIN = 25

    # Wall-clock duration BTC must stay bearish before shorts turn on. Kept in
    # HOURS (not candles) so a timeframe change doesn't silently change the
    # confirmation window: at 1h this was 12 candles, at 4h it is 3 candles.
    SHORT_CONFIRM_HOURS = 12

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        # 200-EMA tide filter (audit 2026-07-23): long only above, short only below.
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        # merge_informative_pair appends "_{timeframe_inf}" to every merged column,
        # so the column name must track self.timeframe (which config.json can override).
        short_ok_col = f"btc_short_ok_{self.timeframe}"

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)
            bear_raw = (
                (btc["close"] < btc["btc_ema200"])
                & (btc["btc_ema50"] < btc["btc_ema200"])
            ).astype(int)
            # confirmed bear = last SHORT_CONFIRM_HOURS worth of candles ALL bear
            confirm_candles = max(
                1, self.SHORT_CONFIRM_HOURS * 60 // timeframe_to_minutes(self.timeframe)
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
        # LONG — trend stack + above 200-EMA + stronger ADX + volume (audit 2026-07-23)
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["ema_fast"])
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["ema_slow"] > dataframe["ema_trend"])
                & (dataframe["close"] > dataframe["ema200"])
                & (dataframe["adx"] > self.ADX_MIN)
                & (dataframe["volume"] > dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # SHORT — bearish stack AND market confirmed in a downtrend AND below 200-EMA
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 1)
                & (dataframe["close"] < dataframe["ema_fast"])
                & (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["ema_slow"] < dataframe["ema_trend"])
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["adx"] > self.ADX_MIN)
                & (dataframe["volume"] > dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                ((dataframe["ema_fast"] < dataframe["ema_slow"])
                 | (dataframe["close"] < dataframe["ema_slow"])
                 | (dataframe["close"] < dataframe["ema200"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        dataframe.loc[
            (
                ((dataframe["ema_fast"] > dataframe["ema_slow"])
                 | (dataframe["close"] > dataframe["ema_slow"])
                 | (dataframe["close"] > dataframe["ema200"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
