# TrendBrake1dFutures — PROTOTYPE. Not wired to any bot/plist. Do not deploy.
#
# WHY THIS EXISTS (2026-07-24 profitability analysis): every intraday/scalp attempt in
# this repo loses (scalp -66%, DayTradeORB -90%, futures LS2 -35% + WF-rejected,
# TrendBrake4h WF-rejected 47%). The ONLY thing that works here is the DAILY brake:
# BrakedHold 1d = +1219%/PF11.06 on crypto, and the 25y equity brake (S&P/Nifty/Gold)
# halves drawdown vs buy&hold. So the brake's edge lives at DAILY granularity, not 4h/1h.
#
# This prototype applies the exact proven brake to FUTURES at 1d (the granularity that
# works): long when close > SMA200 AND ADX>25, exit when close < SMA200. Long-only, no
# stop, no trailing, no shorts. Same template as TrendBrake4h but daily.
#
# CONFIG PRECEDENCE: config_futures.json pins stoploss -0.08 + trailing_stop true, which
# freqtrade applies OVER these attributes. Backtest with the overlay so the pure brake is
# scored: -c config_futures.json -c user_data/prototype_overlay_futures.json
from pandas import DataFrame
import talib.abstract as ta
from freqtrade.strategy import IStrategy


class TrendBrake1dFutures(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = False

    minimal_roi = {"0": 100}       # unreachable; the SMA200 brake is the only exit
    stoploss = -0.99               # effectively off; pure brake
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220     # 200 SMA + ADX warmup

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] > dataframe["sma200"])
            & (dataframe["adx"] > 25)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] < dataframe["sma200"]) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
