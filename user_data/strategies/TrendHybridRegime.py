# TrendHybridRegime — regime-switching futures strategy combining the daily
# 200-SMA brake (long side) with the short-only bear filter (short side).
#
# DESIGN BRIEF (audit 2026-07-28, spec from Claude Code analysis):
#   Three strategies were tested on OKX futures over 6+ years:
#     TrendFollowLS2      (L+S, 4h, ADX35 + BTC gate):  -38.46% / PF 0.81 / DD 45.59%
#     TrendFollowShortOnly(S-only, 4h, BTC bear gate):  -35.15% / PF 0.74 / DD 46.37%
#     TrendBrake1dFutures (L-only, 1d, SMA200 brake):   +21.13% / PF 1.55 / DD 10.76%
#   The short-only wins in the current bear; the daily brake wins over a full
#   cycle. Neither wins in both regimes. This hybrid attempts to capture both
#   by switching sides based on BTC's daily regime.
#
# RULE / PRIORITY ORDER:
#   1. REGIME SWITCH: BTC daily close vs BTC daily SMA200, with a 3% band and
#      3-day confirmation. Three states: BULL (+1), BEAR (-1), NEUTRAL (0).
#      The band + confirmation reduces regime flips from 51 to 14 over 6.3
#      years. NEUTRAL is latched through (last non-neutral state persists for
#      flip-detection) but does NOT trigger new entries.
#   2. LONG SIDE (BULL): pure daily brake — coin daily close > coin daily
#      SMA200 AND daily ADX > 25. No 4h momentum filter (that's what kills the
#      brake). No stop, no trailing — the brake is the exit. A -35% custom
#      stoploss is a LUNA-class tail guard only.
#   3. SHORT SIDE (BEAR): ShortOnly verbatim — coin 4h close < EMA200 AND 4h
#      ADX > 35 AND volume > vol_ma. 8% stop + trailing 3%@6%.
#   4. COOLDOWN: 2 days after every regime flip before the new side can open.
#      Prevents close-short-and-open-long on the same candle at a false cross.
#   5. ASYMMETRIC EXITS: longs exit only on confirmed bear (not neutral);
#      shorts exit on neutral OR coin reclaiming EMA200. Shorts are more
#      trigger-happy because squeezes at bear→bull turns are the fastest loss.
#
# FAILURE CLASSES THIS FIXES:
#   - TrendFollowLS2's 45% DD came from 8% stops firing inside uptrends and
#     from shorting bull-market pullbacks. The hybrid's long side has no stop
#     and the short side only fires in a confirmed BTC bear.
#   - TrendFollowShortOnly's 46% DD came from shorting during bull-to-bear
#     transitions and bear-market squeezes. The 3% band + 3-day confirm +
#     2-day cooldown suppresses transition whipsaws.
#   - TrendBrake1dFutures's -6% in the current bear came from catching
#     dead-cat bounces. The hybrid's short side captures the bear instead.
#
# CONFIG PRECEDENCE: config_futures.json pins stoploss -0.08 + trailing_stop
# true, which freqtrade applies OVER strategy attributes. This would silently
# turn the hybrid back into LS2. ALWAYS backtest with the overlay:
#   -c config_futures.json -c user_data/prototype_overlay_futures.json
# The overlay sets stoploss -0.99 + trailing_stop false, letting the
# custom_stoploss method own all risk policy.

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.strategy.strategy_helper import stoploss_from_open
from pandas import DataFrame


class TrendHybridRegime(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"

    # Risk policy is owned by custom_stoploss; these are overridden by the
    # prototype_overlay_futures.json config in every backtest. The values
    # here are fail-safe defaults if the overlay is missing.
    minimal_roi = {"0": 10}
    stoploss = -0.99
    trailing_stop = False
    use_custom_stoploss = True

    process_only_new_candles = True
    # 200 daily candles == 1200 x 4h, + buffer for ADX warmup on both TFs.
    startup_candle_count = 1300

    # ---- REGIME PARAMETERS ----
    # Band around BTC SMA200 (fractional, e.g. 0.03 = 3%). Audit 2026-07-28:
    # a sweep of band/confirm combinations on BTC daily 2020-07→2026-07 showed
    # 3% band + 3-day confirm reduces flips from 51 (naive cross) to 14, with
    # median regime run 94 days and only 1 sub-10-day regime. 5% buys nothing.
    REGIME_TF = "1d"
    BAND = 0.03
    CONFIRM_DAYS = 3
    COOLDOWN_DAYS = 2

    # ---- ENTRY GATES ----
    LONG_ADX_MIN = 25    # TrendBrake1dFutures's gate (daily ADX)
    SHORT_ADX_MIN = 35   # ShortOnly's gate (4h ADX)

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        # BTC daily for regime; each coin's daily for the long-side brake.
        pairs = [("BTC/USDT:USDT", self.REGIME_TF)]
        if self.dp:
            for p in self.dp.current_whitelist():
                if p != "BTC/USDT:USDT":
                    pairs.append((p, self.REGIME_TF))
        return pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # ---- 4h coin indicators (short side) ----
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        # ---- Coin daily indicators (long side brake) ----
        if self.dp:
            coin_1d = self.dp.get_pair_dataframe(pair, self.REGIME_TF).copy()
            coin_1d["sma200"] = ta.SMA(coin_1d, timeperiod=200)
            coin_1d["adx"] = ta.ADX(coin_1d, timeperiod=14)
            # merge_informative_pair appends "_1d" to each column, so
            # close→close_1d, sma200→sma200_1d, adx→adx_1d.
            dataframe = merge_informative_pair(
                dataframe,
                coin_1d[["date", "close", "sma200", "adx"]],
                self.timeframe, self.REGIME_TF, ffill=True,
            )
        else:
            dataframe["close_1d"] = dataframe["close"]
            dataframe["sma200_1d"] = ta.SMA(dataframe, timeperiod=200)
            dataframe["adx_1d"] = ta.ADX(dataframe, timeperiod=14)

        # ---- BTC daily regime ----
        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.REGIME_TF).copy()
            btc["btc_sma200"] = ta.SMA(btc, timeperiod=200)

            bull_raw = (btc["close"] > btc["btc_sma200"] * (1 + self.BAND)).astype(int)
            bear_raw = (btc["close"] < btc["btc_sma200"] * (1 - self.BAND)).astype(int)

            bull = bull_raw.rolling(self.CONFIRM_DAYS).min().fillna(0).astype(int)
            bear = bear_raw.rolling(self.CONFIRM_DAYS).min().fillna(0).astype(int)

            # 1 BULL, -1 BEAR, 0 NEUTRAL. fillna(0) on the rolling makes the
            # pre-warmup period NEUTRAL → flat (fail-safe, matches LS2).
            btc["btc_regime"] = np.where(bull, 1, np.where(bear, -1, 0))

            # Latch through NEUTRAL for flip detection: replace 0 with the last
            # non-zero value, then count days since the last regime change.
            side = pd.Series(btc["btc_regime"]).replace(0, np.nan).ffill()
            flip = (side != side.shift()).cumsum()
            btc["btc_days_since_flip"] = side.groupby(flip).cumcount()

            dataframe = merge_informative_pair(
                dataframe,
                btc[["date", "btc_regime", "btc_days_since_flip"]],
                self.timeframe, self.REGIME_TF, ffill=True,
            )
        else:
            dataframe["btc_regime_1d"] = 0
            dataframe["btc_days_since_flip_1d"] = 999

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = dataframe["btc_regime_1d"]
        days_since = dataframe["btc_days_since_flip_1d"]
        cooled = days_since >= self.COOLDOWN_DAYS

        # ---- LONG: daily brake, BULL regime, after cooldown ----
        # No 4h momentum filter — TrendBrake1d's edge is its lack of filters.
        dataframe.loc[
            (
                (regime == 1)
                & cooled
                & (dataframe["close_1d"] > dataframe["sma200_1d"])
                & (dataframe["adx_1d"] > self.LONG_ADX_MIN)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        # ---- SHORT: ShortOnly logic, BEAR regime, after cooldown ----
        dataframe.loc[
            (
                (regime == -1)
                & cooled
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["adx"] > self.SHORT_ADX_MIN)
                & (dataframe["volume"] > dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        regime = dataframe["btc_regime_1d"]

        # ---- EXIT LONG: brake OR confirmed bear (NOT neutral) ----
        # Exiting on every 5-day neutral wobble would reintroduce the whipsaw
        # the band exists to suppress.
        dataframe.loc[
            (
                ((dataframe["close_1d"] < dataframe["sma200_1d"])
                 | (regime == -1))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1

        # ---- EXIT SHORT: regime != BEAR (includes NEUTRAL) OR coin > EMA200 ----
        # Asymmetry: shorts exit on NEUTRAL (earlier) because squeezes at
        # bear→bull turns are the fastest loss mode; longs bleed slower at
        # bull→bear turns and the daily brake handles them.
        dataframe.loc[
            (
                ((regime != -1)
                 | (dataframe["close"] > dataframe["ema200"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time,
                        current_rate: float, current_profit: float,
                        after_fill: bool, **kwargs) -> float | None:
        """Side-aware risk policy.

        SHORT: ShortOnly's tested policy — 8% hard stop, trailing 3% armed
        at +6%. This is what produced the +7.92% bear-market result.

        LONG: no trailing, no 8% stop — the daily brake is the exit. -35%
        is a LUNA-class tail guard only, set far outside the brake's normal
        noise so it never fires in normal operation. Bolting an 8% stop onto
        the brake is exactly what turned TrendBrake1d into TrendFollowLS2.
        """
        if trade.is_short:
            if current_profit > 0.06:
                return stoploss_from_open(
                    current_profit - 0.03, current_profit, True, trade.leverage
                )
            return stoploss_from_open(-0.08, current_profit, True, trade.leverage)
        # LONG: -35% tail guard only. is_long=False because stoploss_from_open
        # expects the profit sign from the long perspective; for a long trade
        # current_profit is already in the right convention.
        return stoploss_from_open(-0.35, current_profit, False, trade.leverage)
