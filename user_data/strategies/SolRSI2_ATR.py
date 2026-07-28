# SolRSI2_ATR — RSI-2 extreme with ATR-based custom stop on SOL perp, 1h.
#
# Same entry as SolRSI2_1h but uses a custom stoploss = 2 * ATR(14) instead
# of a fixed 4%. This adapts the stop to current volatility — wider in
# choppy markets, tighter in calm markets.

from freqtrade.strategy import IStrategy, merge_informative_pair, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class SolRSI2_ATR(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    minimal_roi = {"0": 0.02, "360": 0.005, "720": 0.0}
    stoploss = -0.99  # overridden by custom_stoploss
    trailing_stop = False
    use_exit_signal = True
    use_custom_stoploss = True

    process_only_new_candles = True
    startup_candle_count = 500

    rsi_buy = IntParameter(2, 10, default=5, space="buy", optimize=True)
    rsi_exit = IntParameter(40, 70, default=50, space="sell", optimize=True)
    atr_mult = DecimalParameter(1.0, 3.0, default=2.0, decimals=1, space="buy", optimize=True)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["rsi2"] = ta.RSI(df, timeperiod=2)
        df["rsi14"] = ta.RSI(df, timeperiod=14)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["atr"] = ta.ATR(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_tf).copy()
            inf["ema50"] = ta.EMA(inf, timeperiod=50)
            inf["ema200"] = ta.EMA(inf, timeperiod=200)
            df = merge_informative_pair(
                df, inf[["date", "close", "ema50", "ema200"]],
                self.timeframe, self.inf_tf, ffill=True)
        return df

    def custom_stoploss(self, pair: str, trade, current_time, current_rate, current_profit, **kwargs) -> float:
        # ATR-based stop: 2 * ATR at entry time / current price
        # We use the ATR from the entry candle, stored in trade entry
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or len(df) == 0:
            return -0.04  # fallback
        # Find the ATR at trade entry time
        entry_idx = df.index[df["date"] >= pd.Timestamp(trade.open_date_utc, tz="UTC")]
        if len(entry_idx) == 0:
            return -0.04
        atr_at_entry = df.loc[entry_idx[0], "atr"]
        if pd.isna(atr_at_entry) or atr_at_entry <= 0:
            return -0.04
        # Stop = -atr_mult * ATR / entry_price (as a fraction)
        return -(self.atr_mult.value * atr_at_entry / trade.open_rate)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        up_4h = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        dataframe.loc[
            (
                (dataframe["rsi2"] < self.rsi_buy.value)
                & (dataframe["close"] > dataframe["ema50"])
                & up_4h
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["rsi2"] > self.rsi_exit.value) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe


# Need pandas for custom_stoploss
import pandas as pd
