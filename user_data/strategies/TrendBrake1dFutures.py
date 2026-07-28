# TrendBrake1dFutures — daily 200-SMA brake on OKX futures, long-only.
#
# DEPLOYED 2026-07-28 to com.vikas.bot.futures (port 8081) replacing
# TrendFollowLS2, which lost -38.46% / PF 0.81 / DD 45.59% over 6+ years.
# This is the only strategy that survived a full-cycle backtest on the futures
# bot: +21.13% / PF 1.55 / DD 10.76% over 2020-08→2026-07 (4 altcoin perps).
#
# WHY THIS EXISTS (2026-07-24 profitability analysis): every intraday/scalp
# attempt in this repo loses (scalp -66%, DayTradeORB -90%, futures LS2 -38% +
# WF-rejected, TrendBrake4h WF-rejected 47%). The ONLY thing that works here is
# the DAILY brake: BrakedHold 1d = +1219%/PF11.06 on crypto, and the 25y equity
# brake (S&P/Nifty/Gold) halves drawdown vs buy&hold. So the brake's edge lives
# at DAILY granularity, not 4h/1h.
#
# This applies the exact proven brake to FUTURES at 1d (the granularity that
# works): long when close > SMA200 AND ADX>25, exit when close < SMA200.
# Long-only, no stop, no trailing, no shorts.
#
# CAVEAT: backtest priced funding at ~zero (OKX funding API only goes back 3
# months). Hermes research 2026-07-28: longs pay ~15-30%/yr in bulls, ~4-8%/yr
# in bears. The +21% is an UPPER BOUND; real return after funding is lower.
# The brake is still the only positive-expectancy futures strategy tested.
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
