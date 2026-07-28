# ScalpV2v2 — more aggressive filters and wider stop.
#
# RESULTS (audit 2026-07-28):
#   - 4% stoploss reduced stop-outs from 546 to ~40 trades
#   - RSI 20 filter cuts screaming dips that kill the edge
#   - Fewer but higher-quality entries survived; total loss dropped to -5% fee-free
#
# CURRENT ISSUE: win rate up (66%) but signals are too selective.
# IDEA: widen stoploss further to 6% and keep RSI at 28 to generate enough trades.
# Should hit audit targets: >0% returns, >1.3 PF, <30% DD, >100 trades.

import numpy as np
from freqtrade.strategy import (IStrategy, merge_informative_pair,
                                IntParameter, DecimalParameter)
from pandas import DataFrame
import talib.abstract as ta


class ScalpV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "5m"
    inf_tf = "1h"
    can_short = False

    # Much wider stop for 5m volatility, but not so wide that weak losers dominate
    minimal_roi = {"0": 0.02, "180": 0.005, "360": 0.0}
    stoploss = -0.06                 # 2026-07-28: viable range for 5m BTC
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 1300

    # Tight now to preserve signal quality (fewer weaker entries)
    band = DecimalParameter(1.0, 3.0, default=2.0, decimals=1, space="buy", optimize=False)
    adx_max = IntParameter(15, 25, default=20, space="buy", optimize=False)  # tighter
    vol_mult = DecimalParameter(1.2, 1.5, default=1.3, space="buy", optimize=False)  # stronger volume
    rsi_oversold = IntParameter(20, 28, default=28, space="buy", optimize=False)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")

        # --- session-anchored VWAP + expanding deviation (00:00 UTC reset) ---
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_vol = df.groupby(day)["volume"].cumsum()
        cum_tpv = (tp * df["volume"]).groupby(day).cumsum()
        df["vwap"] = cum_tpv / cum_vol.replace(0, np.nan)
        dev = df["close"] - df["vwap"]
        df["vwap_sd"] = dev.groupby(day).transform(
            lambda s: s.expanding(min_periods=12).std())

        df["rsi"] = ta.RSI(df, timeperiod=14)
        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # --- 1h trend filter (future-safe) ---
        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_tf).copy()
            inf["close"] = inf["close"]
            inf["ema50"] = ta.EMA(inf, timeperiod=50)
            inf["ema100"] = ta.EMA(inf, timeperiod=100)
            df = merge_informative_pair(
                df, inf[["date", "close", "ema50", "ema100"]],
                self.timeframe, self.inf_tf, ffill=True)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        up_1h = (dataframe[f"close_{s}"] > dataframe[f"ema100_{s}"]) & \
                (dataframe[f"ema50_{s}"] > dataframe[f"ema100_{s}"])
        lower = dataframe["vwap"] - self.band.value * dataframe["vwap_sd"]
        dataframe.loc[
            (
                (dataframe["close"] < lower)                     # >2σ below fair value
                & (dataframe["rsi"] < self.rsi_oversold.value)
                & (dataframe["adx"] < self.adx_max.value)
                & up_1h
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["close"] >= dataframe["vwap"]) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe