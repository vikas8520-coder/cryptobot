# SolRSI2_1h — RSI-2 extreme reversion with regime filter, 1h, multi-coin.
#
# THESIS: RSI(2) < 9 is extremely oversold — price is likely to snap back.
# Combined with a higher-timeframe uptrend filter (4h EMA50 > EMA200), this
# catches dips in bull markets. Exit when RSI(2) > 52 (momentum restored).
#
# This is a mean-reversion strategy, but on 1h (not 5m) where moves are bigger.
# The key difference from the failed VWAP reversion: RSI-2 < 9 is a much
# more extreme filter than "price below VWAP band" — it only triggers on
# genuine panic selling. Only 3.3% stop-out rate (7 stops in 209 trades).
#
# PORTFOLIO: SOL + LINK + ADA (audit 2026-07-28, army research sweep).
#   Multi-coin screen of 49 coins (Claude Code on Binance data) + 11 coins
#   (Devin on Bitget data) + Ollama predictions + web search. Only 3 coins
#   passed all gates: 3+yr history, >$100M daily volume, walk-forward
#   validated (both halves profitable), positive RSI(2) mean-reversion edge.
#   BTC/ETH/DOGE/AVAX/BNB all FAIL — too trending, RSI<9 = keep falling.
#
# PORTFOLIO BACKTEST (OKX data, 0.05% taker, $50/trade, max 3, 4.6yr):
#   +23.69%, PF 1.60, Sharpe 1.71, Calmar 7.13, CAGR 4.82%, DD 3.85%
#   559 trades, 71.2% win, avg hold 2:42
#   Per-pair: SOL +12.27% (72.7% win) | ADA +6.59% (73.3%) | LINK +4.83% (67.7%)
#   Walk-forward: 2022-2024 +11.1% PF1.6 | 2024-2026 +12.59% PF1.5
#
# SOLO BACKTEST (OKX, SOL-only, $100/trade, 4.6yr):
#   +6.16%, PF 1.98, Sharpe 1.65, Calmar 9.89, DD 0.72%, 209 trades, 72.7% win
#   Walk-forward: 2022-2024 +2.93% PF1.93 | 2024-2026 +3.23% PF2.04
#
# CROSS-EXCHANGE VALIDATION (Bitget data, same fee, same period):
#   +5.85%, PF 1.97, Sharpe 1.62, 208 trades, 70.7% win
#   → Edge is exchange-agnostic (RSI signal, not venue-specific).
#
# STATISTICAL NOTE: No single coin is significant alone (p=0.15-0.35).
#   The portfolio is significant (p=0.0011, Claude Code's calculation).
#   Edge decayed ~50% after early 2024 on all coins including SOL, but
#   both walk-forward halves remain profitable.
#
# INDIA RELOCATION PLAN (audit 2026-07-28):
#   OKX is banned in India (exited Mar 2024, FIU restricted list).
#   Binance is FIU-registered (Aug 2024) and fully legal in India.
#   Bitget paused new India onboarding Feb 2026 but existing users OK.
#   → For India live trading: switch config exchange to Binance.
#   → Strategy is identical; only the exchange name changes.
#   → Binance has same fees as OKX (0.02% maker / 0.05% taker).

from freqtrade.strategy import IStrategy, merge_informative_pair, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class SolRSI2_1h(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    inf_tf = "4h"
    can_short = False

    # ROI: 2% immediate, 0.5% after 6h, exit at 0% after 12h
    minimal_roi = {"0": 0.02, "360": 0.005, "720": 0.0}
    stoploss = -0.04
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True
    use_exit_signal = True

    process_only_new_candles = True
    startup_candle_count = 500

    rsi_buy = IntParameter(2, 10, default=5, space="buy", optimize=True)
    rsi_exit = IntParameter(40, 70, default=50, space="sell", optimize=True)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        return [(p, self.inf_tf) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        df["rsi2"] = ta.RSI(df, timeperiod=2)
        df["rsi14"] = ta.RSI(df, timeperiod=14)
        df["ema50"] = ta.EMA(df, timeperiod=50)
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
        dataframe.loc[
            (
                (dataframe["rsi2"] < self.rsi_buy.value)
                & (dataframe["close"] > dataframe["ema50"])  # still in uptrend
                & up_4h  # 4h confirms uptrend
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
