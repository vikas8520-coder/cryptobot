# BrakedHold — the "hold, but with a brake" strategy.
#
# The one edge that survived honest, fee-aware testing (see risk_backtest.py and
# regime_hold_backtest.py): don't out-trade the market — HOLD each coin while it is
# above its 200-day moving average, step to cash when it falls below. ~9-20 trades a
# year, so fees are irrelevant. It doesn't beat buy&hold on return; it roughly HALVES
# the drawdown (BTC 77%->36%), which is what makes crypto actually holdable — it sat
# in cash through the 2022 (-65%->0%) and 2026 (-34%->0%) crashes.
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class BrakedHold(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False

    # Hold while the trend holds: no ROI target, no stop — the 200-day brake is the
    # ONLY exit. (ROI/stops would re-introduce the turnover we proved is harmful.)
    minimal_roi = {"0": 100}      # effectively disabled (never hits +10000%)
    stoploss = -0.99              # effectively disabled; the brake governs exits
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 220    # need 200 for the SMA + buffer

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # enter/stay long while price is above the 200-day trend brake
        dataframe.loc[dataframe["close"] > dataframe["sma200"], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # step to cash the moment price falls below the brake
        dataframe.loc[dataframe["close"] < dataframe["sma200"], "exit_long"] = 1
        return dataframe
