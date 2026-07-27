# BrakedHoldBufBig20 — backtest-only probe (audit 2026-07-23).
#
# Brake-buffer diagnostic (see BrakedHoldBufBig10): the widest band tested.
# BUF = 0.20 → enter only when close is >20% ABOVE the 200-day line, exit only when
# >20% BELOW it. A 20% band on daily crypto MUST change entry/exit timing if the
# override is live; if Buf00 == BufBig20 even here, the subclass method is not being
# invoked. Freeze-safe: live BrakedHold.py + config_braked.json untouched.
from pandas import DataFrame
from BrakedHold import BrakedHold


class BrakedHoldBufBig20(BrakedHold):
    BUF = 0.20    # 20% band around sma200

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["sma200"] * (1 + self.BUF), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * (1 - self.BUF), "exit_long"] = 1
        return dataframe
