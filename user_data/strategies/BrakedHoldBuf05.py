# BrakedHoldBuf05 — backtest-only probe (audit 2026-07-23).
#
# A/B harness subclass for the brake-buffer (hysteresis) sweep. Overrides ONLY the
# entry/exit band around sma200 via BUF; reuses the parent's sma200 column (no
# recomputed indicators). BUF = 0.005 → enter only when close is >0.5% ABOVE the
# 200-day line, exit only when >0.5% BELOW it, so touches of the line don't whipsaw
# in/out of cash. Freeze-safe: the live BrakedHold.py + config_braked.json untouched.
from pandas import DataFrame
from BrakedHold import BrakedHold


class BrakedHoldBuf05(BrakedHold):
    BUF = 0.005    # 0.5% band around sma200

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma200"] * (1 + self.BUF), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * (1 - self.BUF), "exit_long"] = 1
        return dataframe
