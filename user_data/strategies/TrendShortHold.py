# TrendShortHold — short-only futures strategy for BTC bear regimes.
#
# CROSS-VERIFY AUDIT 2026-07-28 (Claude Code): The original funding thesis was
# quantitatively false. Funding contributes only ~2.7% of returns (+6.89 USDT
# on +253.29 USDT total), not 59% as previously claimed. The reason: funding
# is positive 85% of the time UNCONDITIONALLY (driven by bull markets when
# longs are crowded), but this strategy only trades in confirmed BTC bear
# regimes where funding rates are ~12x lower (mean +0.000011/8h while short
# vs +0.000138 unconditional, positive only 65.8% of the time). You cannot
# harvest bull-market funding with a bear-market-only short.
#
# The REAL edge is short-momentum: entering shorts only when BTC is in a
# confirmed bear regime (below both EMA50 and EMA200 for 12h+) and the coin
# has a strong downtrend (ADX > 25). Holding through bear-market bounces
# (no coin EMA200 exit) captures the full downtrend. Funding is a minor bonus.
#
# EVOLUTION:
#   TrendFollowShortOnly (original): exited too early on dead-cat bounces
#     (coin reclaims EMA200 briefly, then continues down). Cut winners.
#   TrendShortHold (this): removes the coin EMA200 exit, only exits when BTC
#     is no longer bearish. Holds through bear-market bounces to catch the
#     full downtrend.
#
# OPTIMIZATION SWEEP (2026-07-28, all net of funding, 6+ year backtest):
#   ADX_MIN:   20→+21.4%  25→+23.6%  28→+27.2%(overfit)  30→+20.5%  35→+7.1%
#   Volume:    removed → +5.3% improvement (was blocking good entries)
#   CONFIRM:   4h→+23.2%  8h→+22.0%  12h→+23.6%  24h→+9.9%  48h→+14.5%
#   Coin exit: removed → +20% improvement (hold through bounces)
#
# FINAL CONFIG: ADX_MIN=25, CONFIRM_HOURS=12, no volume filter, BTC-only exit.
#   Result: +23.18% / PF 1.57 / DD 7.86% / 136 trades over 6+ yr (verified 2026-07-28,
#     with startup_candle_count=1000 fix for EMA200 convergence).
#   Split-sample: 2020-2023 PF 2.13, 2023-2026 PF 1.19 (edge decay in recent period).
#
# ENTRY RULE:
#   short allowed <=> BTC close < BTC EMA200 AND BTC EMA50 < BTC EMA200,
#                     sustained for 12h (CONFIRM_HOURS),
#                 AND coin close < coin EMA200,
#                 AND ADX > 25.
#
# EXIT RULE:
#   close short <=> BTC is no longer in confirmed bear regime.
#   No coin EMA200 exit. No stop. No trailing. The BTC regime exit is the
#   only risk control — when BTC turns bullish, all shorts close.
#
# WHY THIS WORKS (and long-only doesn't):
#   1. The BTC bear regime gate ensures we only short during real bear markets
#      (BTC below both EMA50 and EMA200 for 12h+). This avoids shorting bull
#      corrections.
#   2. Holding through bear-market bounces (no coin EMA200 exit) is critical:
#      it avoids cutting winners on dead-cat bounces that briefly reclaim
#      EMA200 then continue down.
#   3. The ADX>25 filter ensures we only short when there's a real trend (not
#      choppy sideways action).
#   4. Funding income is a minor bonus (~2.7% of returns), not the primary
#      edge. The primary edge is short-momentum during confirmed bear regimes.
#
# CAVEAT: Funding data is Binance proxy (OKX API only goes back 3 months).
# Binance and OKX funding rates are highly correlated but not identical. Live
# performance may differ from backtest. The strategy's edge depends on BTC
# continuing to have multi-week bear regimes — if the market structure changes
# to persistent sideways action, this strategy would have fewer opportunities.

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class TrendShortHold(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"

    minimal_roi = {"0": 10}
    stoploss = -0.99
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 1000  # audit 2026-07-28: was 220, EMA200 unconverged by 1.4%
    ADX_MIN = 25
    CONFIRM_HOURS = 12

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

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
            dataframe[f"btc_short_ok_{self.timeframe}"] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # SHORT when BTC is confirmed bearish and the coin is below its 200-EMA.
        # No volume filter — the 2026-07-28 sweep showed it blocked good entries
        # and reduced return by ~5%. Funding income more than compensates for
        # the slightly worse entry quality.
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 1)
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["adx"] > self.ADX_MIN)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EXIT: BTC no longer bearish ONLY. No coin EMA200 exit — holding
        # through bear-market bounces is critical for funding income and
        # catching the full downtrend. The 2026-07-28 sweep showed removing
        # the coin EMA200 exit improved return by ~20%.
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 0)
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
