# TrendFollowLS2 — long+short, BOTH sides gated by BTC market-regime filters.
#
# TrendFollowLS problem: it shorted during bull-market PULLBACKS and got
# squeezed, cutting the bull return (+5.2% vs long-only +7.8%). The shorts
# themselves are fine in a bear; the issue is shorting inside an uptrend.
# Audit 2026-07-27: on the OKX 4h futures backtest longs ALSO lost money in a
# bear market (-4.73%), so longs now require a confirmed BTC uptrend too.
#
# FIX:
#   long  allowed <=> BTC close > BTC EMA200 AND BTC EMA50 > BTC EMA200,
#                       sustained for 12h (so a brief BTC pop inside a bear
#                       doesn't switch longs on).
#   short allowed <=> BTC close < BTC EMA200 AND BTC EMA50 < BTC EMA200,
#                       sustained for 12h (so a brief BTC dip inside a bull
#                       doesn't switch shorts on).
# In a bull, shorts are disabled; in a bear, longs are disabled.

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
    # Minimum ADX for entry. Audit 2026-07-27: ADX sweep on OKX 4h futures
    # (LINK/AVAX/LTC/ADA, train 2025-07-20→2026-01-20, hold-out 2026-01-20→2026-07-20)
    # showed ADX35 beats 20/25/30 on PF, expectancy and max-drawdown in BOTH splits.
    # ADX35 train: PF 0.82 / DD 3.38%; hold-out: PF 1.73 / DD 4.40% vs ADX25 1.24/7.48%.
    ADX_MIN = 35

    # Wall-clock duration BTC must stay above/below its EMAs before long/short
    # entries turn on. Kept in HOURS (not candles) so a timeframe change doesn't
    # silently change the confirmation window: at 1h this is 12 candles, at 4h 3.
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
        # 200-EMA tide filter (audit 2026-07-23): long only above, short only below.
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        # merge_informative_pair appends "_{timeframe_inf}" to every merged column,
        # so the column name must track self.timeframe (which config.json can override).
        long_ok_col = f"btc_long_ok_{self.timeframe}"
        short_ok_col = f"btc_short_ok_{self.timeframe}"

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)

            bull_raw = (
                (btc["close"] > btc["btc_ema200"])
                & (btc["btc_ema50"] > btc["btc_ema200"])
            ).astype(int)
            bear_raw = (
                (btc["close"] < btc["btc_ema200"])
                & (btc["btc_ema50"] < btc["btc_ema200"])
            ).astype(int)

            # confirmed trend = last CONFIRM_HOURS worth of candles ALL in the regime
            confirm_candles = max(
                1, self.CONFIRM_HOURS * 60 // timeframe_to_minutes(self.timeframe)
            )
            btc["btc_long_ok"] = bull_raw.rolling(confirm_candles).min().fillna(0).astype(int)
            btc["btc_short_ok"] = bear_raw.rolling(confirm_candles).min().fillna(0).astype(int)

            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_long_ok", "btc_short_ok"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            # No BTC regime data: stay flat rather than trade ungated.
            dataframe[long_ok_col] = 0
            dataframe[short_ok_col] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # LONG — trend stack + above 200-EMA + confirmed BTC uptrend + ADX + volume
        dataframe.loc[
            (
                (dataframe[f"btc_long_ok_{self.timeframe}"] == 1)
                & (dataframe["close"] > dataframe["ema_fast"])
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
