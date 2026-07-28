# TrendShortFunding — short altcoin perpetuals to harvest funding + catch crashes.
#
# KEY INSIGHT (2026-07-28 funding audit): Shorts RECEIVE funding (longs pay
# shorts 85% of the time). The TrendFollowShortOnly was -35% gross but only
# -3.01% net of funding — funding income added +32% over 6 years. This strategy
# makes funding the PRIMARY signal: short when funding is high (crowded longs,
# expensive carry) to maximize funding income AND catch the crash that high
# funding predicts (BIS WP 1087: "high crypto carry predicts future price
# crashes").
#
# RULE / PRIORITY ORDER:
#   1. FUNDING SIGNAL: current 8h funding rate > FUNDING_MIN (0.02%/8h).
#      This is the primary gate. High funding = longs crowded = crash risk +
#      we collect the high funding while holding the short.
#   2. TREND CONFIRMATION: coin 4h close < EMA200. Don't short into uptrends
#      even if funding is high — the funding income won't offset the price rise.
#   3. NO BTC REGIME GATE: unlike TrendFollowShortOnly, we don't require BTC
#      to be in a bear regime. High funding can occur at blow-off tops in bull
#      markets (e.g., Nov 2021, March 2024) — those are the best shorting
#      opportunities. The funding signal + coin downtrend is sufficient.
#   4. NO ADX GATE: the funding rate IS the signal. Adding ADX would filter
#      out the best entries (high funding often happens at tops before the
#      trend has clearly reversed, so ADX might not be high yet).
#   5. EXIT: funding drops below FUNDING_EXIT (0.01%/8h = default rate) OR
#      coin reclaims EMA200. Exit when funding income dries up or the trend
#      reverses.
#   6. NO STOP, NO TRAILING: stops cut winners short and reduce holding time
#      (= less funding collected). The EMA200 exit is the risk control.
#
# WHY THIS MIGHT BE THE FIRST PROFITABLE FUTURES STRATEGY:
#   - The short-only with BTC bear gate was -3% net over 6 years. It collected
#     ~+32% in funding but lost ~-35% on trades (stop-losses, false entries).
#   - This strategy targets the SAME funding income but with better entries
#     (high funding = crash coming) and fewer stops (hold longer = more funding).
#   - If the trading loss can be reduced from -35% to even -20%, the funding
#     income (+32% or more, since we enter when funding is highest) would push
#     the strategy solidly positive.

from pandas import DataFrame
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from freqtrade.enums import CandleType


class TrendShortFunding(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"

    minimal_roi = {"0": 100}
    stoploss = -0.99
    trailing_stop = False
    use_custom_stoploss = False

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220

    # ---- FUNDING PARAMETERS ----
    # Audit 2026-07-28: BTC funding median = 0.01%/8h, 90th pct = 0.024%/8h.
    # FUNDING_MIN: only short when funding > this (targets crowded longs).
    # FUNDING_EXIT: exit when funding drops below this (income dries up).
    FUNDING_MIN = 0.0002    # 0.02%/8h = above median; crowded longs
    FUNDING_EXIT = 0.0001   # 0.01%/8h = default rate; income normalized

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, entry_tag, side, **kwargs) -> float:
        return 1.0

    def informative_pairs(self):
        pairs = []
        if self.dp:
            for p in self.dp.current_whitelist():
                pairs.append((p, "1h"))
        return pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # ---- 4h coin indicators ----
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()

        # ---- Funding rate (merged from 1h to 4h via merge_asof) ----
        if self.dp:
            funding = self.dp.get_pair_dataframe(
                pair, "1h", candle_type=CandleType.FUNDING_RATE
            ).copy()
            if len(funding) > 0:
                # Use merge_asof to forward-fill funding rates into 4h candles.
                # Shift by 8h to avoid lookahead (only see completed funding).
                funding = funding[["date", "open"]].rename(
                    columns={"open": "funding_rate"}
                ).sort_values("date")
                funding["date"] = funding["date"] + pd.Timedelta(hours=8)
                # Cast both to ns to avoid merge_asof dtype mismatch (ms vs us).
                dataframe = dataframe.sort_values("date")
                dataframe["date"] = dataframe["date"].astype("datetime64[ns, UTC]")
                funding["date"] = funding["date"].astype("datetime64[ns, UTC]")
                dataframe = pd.merge_asof(
                    dataframe, funding,
                    on="date", direction="backward",
                )
                dataframe["funding_rate"] = dataframe["funding_rate"].fillna(0.0)
                dataframe = dataframe.sort_values("date").reset_index(drop=True)
            else:
                dataframe["funding_rate"] = 0.0
        else:
            dataframe["funding_rate"] = 0.0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # SHORT: funding high (crowded longs) AND coin in downtrend
        dataframe.loc[
            (dataframe["funding_rate"] > self.FUNDING_MIN)
            & (dataframe["close"] < dataframe["ema200"])
            & (dataframe["volume"] > 0),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EXIT: funding normalized OR coin reclaims EMA200
        dataframe.loc[
            ((dataframe["funding_rate"] < self.FUNDING_EXIT)
             | (dataframe["close"] > dataframe["ema200"]))
            & (dataframe["volume"] > 0),
            "exit_short",
        ] = 1
        return dataframe
