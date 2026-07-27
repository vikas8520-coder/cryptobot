# TrendBrake4h — PROTOTYPE. Not wired to any bot/plist. Do not deploy.
#
# WHY THIS EXISTS (futures walk-forward 2026-07-23): TrendFollowLS2 is BROKEN, not
# unlucky — only 8/23 non-2022 out-of-sample windows are positive (35%), so the
# 2022 crash was an alibi, not the cause. What it does wrong is what every dead
# strategy in this repo does wrong: it churns. Long AND short, an -8% stop, and a
# trailing stop that harvests every wiggle => lots of small round trips, each
# paying fees and funding, none of them holding a real trend.
#
# So this prototype imports the ONE concept in this repo that survived honest
# testing (BrakedHold, ~9-20 trades/yr, halved BTC drawdown 77%->36%) and applies
# it at 4h on futures pairs:
#     enter  = close > SMA200  AND  ADX > 25   (trend exists AND is confirmed)
#     exit   = close < SMA200                  (the brake — the ONLY exit)
# No stop, no trailing, no shorts. The whole thesis is that the exit is the
# trend ending, so any stop/trail that fires first is strictly noise-harvesting.
#
# ADX > 25 is the only addition over BrakedHold: at 4h (vs 1d) the SMA200 is just
# ~33 days, so price chops across it far more often, and the ADX gate is what
# stops the brake from being re-armed inside a directionless range.
#
# CONFIG PRECEDENCE WARNING: config_futures.json pins stoploss -0.08 AND
# trailing_stop true, which freqtrade applies OVER these attributes. Backtesting
# against the bare live config would therefore test a trailing-stop strategy that
# merely looks like a brake. Always run with the overlay:
#   -c config_futures.json -c user_data/prototype_overlay_futures.json
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class TrendBrake4h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "4h"
    can_short = False              # long-only: the shorts are half of why LS2 bled

    # All three disabled deliberately — the 200-SMA brake is the only exit.
    minimal_roi = {"0": 100}       # unreachable (never hits +10000%)
    stoploss = -0.99               # effectively off; do NOT "fix" this
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 220     # 200 for the SMA + ADX warmup + buffer

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Above the brake AND actually trending — ADX is what keeps 4h chop out.
        dataframe.loc[
            (dataframe["close"] > dataframe["sma200"])
            & (dataframe["adx"] > 25)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Step to cash the moment price falls below the brake. No ADX condition
        # here on purpose: a fading trend must still be exited, not held.
        dataframe.loc[
            (dataframe["close"] < dataframe["sma200"]) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
