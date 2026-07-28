# SolScalp1h — BB squeeze breakout on SOL/USDT:USDT perp, 1h timeframe.
#
# DESIGN 2026-07-28 (informed by 3-agent analysis):
#   - Ollama, Claude Code, and web search all agree: OKX futures is the right
#     venue (0.05% taker vs 0.1% binanceus spot — halves the fee hurdle).
#   - Claude Code measured: SOL has the best move-to-fee ratio of any liquid
#     major (fee = 22-23% of median 1h move vs 30% ETH, 41-50% BTC).
#   - Claude Code found: direction isn't predictable (lag-1 autocorr ~0), but
#     volatility IS (autocorr +0.21-0.36, stable across halves). This means
#     breakout strategies (conditioning on volatility expansion) are
#     structurally correct; mean-reversion (conditioning on direction) is not.
#   - Break-even win rate at 1h taker: 57.3%. With maker-in/taker-out: 55.1%.
#
# STRATEGY:
#   - 1h BB(20, 2σ): identify squeeze when BB width < 50% of 100-bar avg
#   - Entry: price breaks above upper BB after a squeeze, with volume + ADX
#   - Exit: price closes below lower BB (let winners run to bottom of band)
#   - 4h trend filter: EMA50 > EMA200 (only trade with the higher TF tide)
#   - ROI: 3% immediate, 1% after 6h, 0% after 12h (time-decay backstop)
#   - Stop: 4% (SOL 1h volatility is ~0.7%, so 4% = ~5.7 ATR)
#   - Long-only (SOL perp; can_short=False for now)
#
# FUNDING: Negligible at 2-6h holds (~0.001% per trade). OKX funding API only
# has 3 months of history, but for this hold duration it doesn't matter.
#
# DATA: OKX SOL/USDT:USDT 1h from 2022-01-01 (4.6 years, 40k candles).

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class SolScalp1h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    # ROI: 3% immediate, 1% after 6h, exit at 0% after 12h
    minimal_roi = {"0": 0.03, "360": 0.01, "720": 0.0}
    stoploss = -0.04
    # Trailing stop: only activates after 1.5% profit, trails at 1.5% behind peak
    # This protects winners but doesn't kill trades that haven't moved yet
    trailing_stop = True
    trailing_stop_positive = 0.015
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True
    use_exit_signal = False  # audit: exit_signal (close < lower BB) lost -1.89% on 6 trades, 0% win

    process_only_new_candles = True
    startup_candle_count = 500  # 4h EMA200 needs ~800 1h candles to converge

    BB_PERIOD = 20
    BB_STD = 2.0
    SQUEEZE_RATIO = 0.5

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        upper, middle, lower = ta.BBANDS(df["close"], timeperiod=self.BB_PERIOD,
                                          nbdevup=self.BB_STD, nbdevdn=self.BB_STD)
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        df["bb_width"] = (upper - lower) / middle
        df["bb_width_avg"] = df["bb_width"].rolling(100).mean()
        df["squeeze"] = (df["bb_width"] < self.SQUEEZE_RATIO * df["bb_width_avg"]).astype(int)
        df["recent_squeeze"] = df["squeeze"].rolling(10).max()
        df["adx"] = ta.ADX(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()
        df["ema50"] = ta.EMA(df, timeperiod=50)

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
        # Entry: breakout candle closes above upper BB (not just touches),
        # AND previous candle was below upper BB (confirmed breakout, not gap),
        # AND the breakout candle is strong (close > open, body > 50% of range)
        prev_below = dataframe["close"].shift(1) < dataframe["bb_upper"].shift(1)
        strong_candle = (dataframe["close"] > dataframe["open"]) & \
                        ((dataframe["close"] - dataframe["open"]) > 0.5 * (dataframe["high"] - dataframe["low"]))
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upper"])
                & prev_below
                & strong_candle
                & (dataframe["recent_squeeze"] == 1)
                & (dataframe["adx"] > 25)
                & (dataframe["volume"] > 2.0 * dataframe["vol_ma"])
                & (dataframe["close"] > dataframe["ema50"])
                & up_4h
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            ((dataframe["close"] < dataframe["bb_lower"]) & (dataframe["volume"] > 0)),
            "exit_long",
        ] = 1
        return dataframe
