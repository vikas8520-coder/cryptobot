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
# audit 2026-07-23 (money-bleed fix):
#   The Freqtrade sibling TrendFollowHopt.json was auto-loading overfit params
#   (stoploss -0.27, trailing only after +39.7%, buy_adx 15). LIVE spot paper:
#   4 wins / 22 losses, profit factor 0.16, EVERY close was exit_signal with avg
#   loss. That JSON is rewritten to hand-designed risk, and entries now also
#   require close > EMA200 + volume confirmation so we quit buying chop.

from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
import talib.abstract as ta


class TrendFollowHopt(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    minimal_roi = {"0": 10}          # never auto-cap a winner
    # Live values come from TrendFollowHopt.json when present; these are the
    # designed defaults if the JSON is missing/deleted.
    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.03
    trailing_stop_positive_offset = 0.06
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 220       # need EMA200 warm

    # --- tunable parameters ---
    ema_fast = IntParameter(10, 30, default=20, space="buy", optimize=True)
    ema_slow = IntParameter(40, 80, default=50, space="buy", optimize=True)
    buy_adx = IntParameter(15, 35, default=25, space="buy", optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Compute every EMA length in the search range (collapses to one value
        # once params are fixed after optimization).
        for v in self.ema_fast.range:
            dataframe[f"ema_fast_{v}"] = ta.EMA(dataframe, timeperiod=v)
        for v in self.ema_slow.range:
            dataframe[f"ema_slow_{v}"] = ta.EMA(dataframe, timeperiod=v)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        # Higher-TF tide: only long above the 200-EMA so we do not buy counter-trend
        # dumps (audit 2026-07-23 money bleed).
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        ef = dataframe[f"ema_fast_{self.ema_fast.value}"]
        es = dataframe[f"ema_slow_{self.ema_slow.value}"]
        dataframe.loc[
            (
                (dataframe["close"] > ef)
                & (ef > es)
                & (es > dataframe["ema_trend"])
                & (dataframe["close"] > dataframe["ema200"])   # long only above 200-EMA
                & (dataframe["adx"] > self.buy_adx.value)
                & (dataframe["volume"] > dataframe["vol_ma"])  # skip dead candles
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
                ((ef < es) | (dataframe["close"] < es) | (dataframe["close"] < dataframe["ema200"]))
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
