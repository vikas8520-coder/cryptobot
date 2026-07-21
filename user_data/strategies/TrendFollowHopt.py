# TrendFollowHopt — TrendFollow with its levers exposed to hyperopt (Optuna).
#
# Tunable:
#   - ema_fast / ema_slow : the trend-stack MA lengths       (space: buy)
#   - buy_adx             : how strong the trend must be      (space: buy)
#   - stoploss + trailing : the "let winners run" dials       (--spaces stoploss trailing)
# ROI stays disabled ({"0": 10}) on purpose — capping winners is the exact
# mistake the trend-follower exists to avoid, so we don't let hyperopt add a cap.
#
# METHOD: train on PART of the bull run, hold out the rest. If the tuned params
# still work on the unseen slice, the gain is real; if they crater, it overfit.
#
# ⚠️ LIVE-VALUE OVERRIDE (audit 2026-07-19, medium): Freqtrade AUTO-LOADS the sibling
# file TrendFollowHopt.json and its values WIN over the class attributes below. So the
# stoploss actually running is -0.27 (not the -0.10 written here), with trailing
# 0.305 / offset 0.397 — i.e. trailing only arms after +39.7%, which on 1h trades is
# essentially never, so every trade rides a -27% hard stop. Those look OVERFIT; the
# hand-designed intent is the -0.10 below. DECISION DEFERRED to when the spot bot
# clears the signal gate: either revert to the designed -0.10 (delete/rename the JSON)
# or re-hyperopt on a clean holdout. Until then the numbers below are DOCUMENTED as
# not-live so this file can no longer mislead. Nothing here is changed silently.

from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowHopt(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    minimal_roi = {"0": 10}          # never auto-cap a winner
    # ⚠️ NOT LIVE — TrendFollowHopt.json overrides these at runtime (see header).
    # Live values: stoploss -0.27, trailing 0.305 / offset 0.397. Designed intent below.
    stoploss = -0.10
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 120

    # --- tunable parameters ---
    ema_fast = IntParameter(10, 30, default=20, space="buy", optimize=True)
    ema_slow = IntParameter(40, 80, default=50, space="buy", optimize=True)
    buy_adx = IntParameter(15, 35, default=20, space="buy", optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Compute every EMA length in the search range (collapses to one value
        # once params are fixed after optimization).
        for v in self.ema_fast.range:
            dataframe[f"ema_fast_{v}"] = ta.EMA(dataframe, timeperiod=v)
        for v in self.ema_slow.range:
            dataframe[f"ema_slow_{v}"] = ta.EMA(dataframe, timeperiod=v)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ef = dataframe[f"ema_fast_{self.ema_fast.value}"]
        es = dataframe[f"ema_slow_{self.ema_slow.value}"]
        dataframe.loc[
            (
                (dataframe["close"] > ef)
                & (ef > es)
                & (es > dataframe["ema_trend"])
                & (dataframe["adx"] > self.buy_adx.value)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ef = dataframe[f"ema_fast_{self.ema_fast.value}"]
        es = dataframe[f"ema_slow_{self.ema_slow.value}"]
        dataframe.loc[
            (
                ((ef < es) | (dataframe["close"] < es))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
