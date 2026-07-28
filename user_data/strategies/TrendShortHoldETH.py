# TrendShortHoldETH — ETH-tuned variant of TrendShortHold.
#
# CROSS-VERIFY AUDIT 2026-07-28 (Claude Code): Funding contributes only ~1.1%
# of returns (+1.24 USDT on +112.92 USDT total). The strategy is pure
# short-momentum, not funding-carry. Additionally, ETH mark data only starts
# 2024-09, so 31 of 47 trades have zero funding fees modeled — the
# "funding-aware" label is inaccurate for 2020-2024. The returns are real
# but come from short-momentum during BTC bear regimes, not funding income.
#
# ETH-specific parameter sweep on 6+ years of data (2020-01 to 2026-07)
# showed ETH prefers stricter ADX and shorter confirmation than the alts:
#   - ADX_MIN=30 (vs 25 for alts): ETH is less volatile, so a stricter trend
#     filter screens out more noise. On alts ADX=30 was worse (+20.5% vs +23.6%),
#     but on ETH it's the best (PF 1.92, DD 2.84%).
#   - CONFIRM_HOURS=4 (vs 12 for alts): ETH bear-market bounces are shorter;
#     waiting 12h misses the optimal entry window. On alts CONF=4 gave +23.2%
#     (close to the 12h optimum of +23.6%), but on ETH CONF=4 is clearly best.
#
# ETH SWEEP RESULT (25 combos, 2020-2026):
#   ADX=30 CONF=4:  +9.03% / PF 1.82 / DD 2.77% / 44 trades / win 43.2% (verified 2026-07-28,
#     with startup_candle_count=1000 fix + extended mark data 2020-2024)
#   ADX=20 CONF=4:  +12.38% / PF 1.68 / DD 4.07% / 83 trades / win 37.3  (more noise)
#   ADX=25 CONF=12: +6.85% / PF 1.40 / DD 5.04% / 58 trades / win 34.5  (alt default)
#
# ROBUSTNESS (Claude Code cross-verify):
#   - Parameter plateau, not a spike: all 9 nearby combos profitable (PF 1.40-1.92)
#   - Split-sample stable: 2020-2023 PF 2.02, 2023-2026 PF 2.04
#
# CAVEAT: 47 trades is a small sample (p-value 0.16, not significant at 5%).
# ETH went up +937% over the backtest period, so shorting it fights a much
# stronger uptrend than the alts. CAGR ~1.8% vs the alt bot's 3.39%.
# See parent TrendShortHold.py for the full strategy rationale.

from freqtrade.exchange import timeframe_to_minutes
from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class TrendShortHoldETH(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"

    minimal_roi = {"0": 10}
    stoploss = -0.99
    trailing_stop = False

    process_only_new_candles = True
    startup_candle_count = 1000  # audit 2026-07-28: was 220, EMA200 unconverged by 1.4%
    # ETH-tuned: stricter ADX (30 vs 25) and shorter confirmation (4h vs 12h).
    # ETH is less volatile than small-cap alts; the stricter filter screens
    # out more noise and the shorter confirm catches ETH's quicker bear moves.
    ADX_MIN = 30
    CONFIRM_HOURS = 4

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        return [("BTC/USDT:USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT:USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)

            bear_raw = (
                (btc["close"] < btc["btc_ema200"])
                & (btc["btc_ema50"] < btc["btc_ema200"])
            ).astype(int)

            confirm_candles = max(
                1, self.CONFIRM_HOURS * 60 // timeframe_to_minutes(self.timeframe)
            )
            btc["btc_short_ok"] = bear_raw.rolling(confirm_candles).min().fillna(0).astype(int)

            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_short_ok"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            dataframe[f"btc_short_ok_{self.timeframe}"] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 1)
                & (dataframe["close"] < dataframe["ema200"])
                & (dataframe["adx"] > self.ADX_MIN)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe[f"btc_short_ok_{self.timeframe}"] == 0)
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1
        return dataframe
