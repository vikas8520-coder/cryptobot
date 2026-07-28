# SolMomPB_1h — Momentum pullback entry on SOL perp, 1h.
#
# THESIS: In a confirmed uptrend, enter on a pullback to EMA20 when RSI
# dips to ~40, then resumes up. Exit when price hits EMA50 (momentum loss).
# This trades WITH the trend, entering on temporary weakness.

from freqtrade.strategy import IStrategy, merge_informative_pair, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class SolMomPB_1h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    minimal_roi = {"0": 0.03, "360": 0.01, "720": 0.0}
    stoploss = -0.04
    trailing_stop = True
    trailing_stop_positive = 0.015
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True
    use_exit_signal = True

    process_only_new_candles = True
    startup_candle_count = 500

    rsi_low = IntParameter(30, 45, default=40, space="buy", optimize=False)
    rsi_high = IntParameter(55, 70, default=60, space="buy", optimize=False)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["ema20"] = ta.EMA(df, timeperiod=20)
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["rsi"] = ta.RSI(df, timeperiod=14)
        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], self.inf_tf).copy()
            inf["ema50"] = ta.EMA(inf, timeperiod=50)
            inf["ema200"] = ta.EMA(inf, timeperiod=200)
            df = merge_informative_pair(
                df, inf[["date", "close", "ema50", "ema200"]],
                self.timeframe, self.inf_tf, ffill=True)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        up_4h = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        # Pullback: RSI was below threshold, now closes back above EMA20
        pullback = (dataframe["rsi"].shift(1) < self.rsi_low.value) & \
                   (dataframe["close"] > dataframe["ema20"]) & \
                   (dataframe["rsi"] < self.rsi_high.value)
        dataframe.loc[
            (
                up_4h
                & pullback
                & (dataframe["adx"] > 20)
                & (dataframe["close"] > dataframe["ema50"])
                & (dataframe["volume"] > 1.0 * dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        trend_break = dataframe[f"ema50_{s}"] < dataframe[f"ema200_{s}"]
        momentum_loss = dataframe["close"] < dataframe["ema50"]
        dataframe.loc[
            ((trend_break | momentum_loss) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
