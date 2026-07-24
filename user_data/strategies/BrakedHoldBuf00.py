# BrakedHoldBuf00 — backtest-only probe (audit 2026-07-23).
#
# A/B harness subclass for the brake-buffer (hysteresis) sweep. Overrides ONLY the
# entry/exit band around sma200 via BUF; reuses the parent's sma200 column (no
# recomputed indicators). BUF = 0.0 == the live hard-cross baseline — a sanity
# duplicate that MUST match the live BrakedHold run exactly. Freeze-safe: the live
# BrakedHold.py + config_braked.json are untouched.
from pandas import DataFrame
from BrakedHold import BrakedHold


class BrakedHoldBuf00(BrakedHold):
    BUF = 0.0    # 0.0% band = current hard sma200 cross (baseline)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma200"] * (1 + self.BUF), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * (1 - self.BUF), "exit_long"] = 1
        return dataframe
