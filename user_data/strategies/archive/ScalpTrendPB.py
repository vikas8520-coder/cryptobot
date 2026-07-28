# ScalpTrendPB — Trend pullback entry on 15m.
#
# THESIS 2026-07-28: VWAP reversion fails because it fights momentum. Instead,
# trade WITH the trend: enter on a pullback to EMA20 within a confirmed uptrend,
# exit when the trend resumes. This should have a higher win rate because we're
# trading with the higher-timeframe tide, not against it.
#
# DESIGN:
#   - 1h trend gate: EMA50 > EMA200 (uptrend confirmed)
#   - 15m entry: price pulls back to touch EMA20, then resumes up (close > EMA20)
#   - 15m exit: price hits EMA50 (momentum loss) or 1h trend breaks
#   - Stop: 3% (wider than 5m scalp — 15m has bigger candles)
#   - ROI: 2% immediate, 1% after 4h, 0% after 12h (time-decay backstop)
#
# FEE AWARENESS: 15m moves are ~0.5-1.5%, so a 2% ROI target gives room above
# the 0.2% round-trip fee. Fewer trades than 5m = less fee drag.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class ScalpTrendPB(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    inf_tf = "1h"
    can_short = False

    minimal_roi = {"0": 0.02, "240": 0.01, "720": 0.0}
    stoploss = -0.03
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 250

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
        # 1h uptrend: EMA50 > EMA200, price above EMA200
        up_1h = (dataframe[f"ema50_{s}"] > dataframe[f"ema200_{s}"]) & \
                (dataframe[f"close_{s}"] > dataframe[f"ema200_{s}"])
        # 15m pullback: price was below/at EMA20, now closes back above it
        # (yesterday's low was below EMA20, today's close is above)
        pullback = (dataframe["low"].shift(1) < dataframe["ema20"].shift(1)) & \
                   (dataframe["close"] > dataframe["ema20"])
        # Not overbought, not in strong downtrend
        dataframe.loc[
            (
                up_1h
                & pullback
                & (dataframe["rsi"] > 30) & (dataframe["rsi"] < 70)
                & (dataframe["adx"] > 15)
                & (dataframe["volume"] > 1.0 * dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        s = self.inf_tf
        # Exit when 1h trend breaks OR price hits EMA50 (momentum loss)
        trend_break = dataframe[f"ema50_{s}"] < dataframe[f"ema200_{s}"]
        momentum_loss = dataframe["close"] < dataframe["ema50"]
        dataframe.loc[
            ((trend_break | momentum_loss) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
