# BrakedHoldBuf20 — backtest-only probe (audit 2026-07-23).
#
# A/B harness subclass for the brake-buffer (hysteresis) sweep. Overrides ONLY the
# entry/exit band around sma200 via BUF; reuses the parent's sma200 column (no
# recomputed indicators). BUF = 0.02 → enter only when close is >2.0% ABOVE the
# 200-day line, exit only when >2.0% BELOW it — the widest band tested, most churn
# reduction but latest entries/exits. Freeze-safe: live BrakedHold.py + config untouched.
from pandas import DataFrame
from BrakedHold import BrakedHold


class BrakedHoldBuf20(BrakedHold):
    BUF = 0.02    # 2.0% band around sma200

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma200"] * (1 + self.BUF), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * (1 - self.BUF), "exit_long"] = 1
        return dataframe
