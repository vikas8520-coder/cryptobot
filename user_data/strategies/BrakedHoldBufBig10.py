# BrakedHoldBufBig10 — backtest-only probe (audit 2026-07-23).
#
# Brake-buffer diagnostic: the small-band sweep (Buf00/05/10/20 = 0-2%) returned
# IDENTICAL numbers, so we test a DELIBERATELY LARGE band to prove the override
# mechanism actually reaches the fill logic. BUF = 0.10 → enter only when close is
# >10% ABOVE the 200-day line, exit only when >10% BELOW it. If trade count still
# does not move vs Buf00, the override isn't being applied (not a band-size issue).
# Freeze-safe: live BrakedHold.py + config_braked.json untouched.
from pandas import DataFrame
from BrakedHold import BrakedHold


class BrakedHoldBufBig10(BrakedHold):
    BUF = 0.10    # 10% band around sma200

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma200"] * (1 + self.BUF), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * (1 - self.BUF), "exit_long"] = 1
        return dataframe
