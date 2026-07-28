# TrendBrake1dFundingGated — daily 200-SMA brake with funding rate entry filter.
#
# DEPLOYMENT NOTE 2026-07-28: TrendBrake1dFutures was +21.13% gross but -9.85%
# net of funding over 6+ years. Funding ate ~31% of returns because the brake
# holds positions for avg 37 days, and BTC funding averages ~12%/yr (85%
# positive — longs pay shorts). This variant gates entries on funding rate to
# avoid the most expensive holding periods.
#
# RULE / PRIORITY ORDER:
#   1. DAILY BRAKE: coin daily close > coin daily SMA200 AND daily ADX > 25.
#      (unchanged from TrendBrake1dFutures — the brake is the core edge)
#   2. FUNDING GATE: only enter long when current 8h funding rate < FUNDING_MAX.
#      This avoids entering when longs are most crowded (and funding most
#      expensive). FUNDING_MAX = 0.01%/8h = the median/default rate.
#   3. FUNDING EXIT: exit if funding rate exceeds FUNDING_EXIT while holding,
#      even if price is still above SMA200. FUNDING_EXIT = 0.03%/8h = ~90th pct.
#   4. BRAKE EXIT: coin daily close < SMA200 (unchanged).

from pandas import DataFrame
import pandas as pd
import talib.abstract as ta
from freqtrade.strategy import IStrategy, merge_informative_pair
from freqtrade.enums import CandleType


class TrendBrake1dFundingGated(IStrategy):
    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "1d"

    minimal_roi = {"0": 100}
    stoploss = -0.99
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220

    # ---- FUNDING GATE PARAMETERS ----
    # Audit 2026-07-28: The top 5-8% of funding periods carry 50% of total
    # funding cost. Hermes research: funding >0.05%/8h predicts crashes (BIS
    # WP 1087). First attempt with 0.01%/8h gate was too aggressive — it
    # filtered out the best trends (high funding = strong bull = best moves).
    # These thresholds target only the extreme tail.
    FUNDING_MAX = 0.0003    # 0.03%/8h = ~90th pct; skips top ~8% (50% of cost)
    FUNDING_EXIT = 0.0005   # 0.05%/8h = crash-warning zone per BIS paper

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

        # ---- Daily brake indicators ----
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        # ---- Funding rate (resampled to daily from 1h funding_rate data) ----
        if self.dp:
            funding = self.dp.get_pair_dataframe(
                pair, "1h", candle_type=CandleType.FUNDING_RATE
            ).copy()
            if len(funding) > 0:
                # Resample 8h funding to daily: take max funding of the day
                # for the entry gate, and last funding of the day for exit.
                funding["date"] = funding["date"].dt.floor("1D")
                funding_daily = funding.groupby("date")["open"].agg(
                    funding_max="max", funding_latest="last"
                ).reset_index()
                # Manual merge: left join on date, ffill gaps.
                # Shift by 1 day to avoid lookahead (only see completed days).
                funding_daily["date"] = funding_daily["date"] + pd.Timedelta(days=1)
                dataframe = dataframe.merge(
                    funding_daily, on="date", how="left"
                )
                dataframe["funding_max"] = dataframe["funding_max"].ffill().fillna(0.0)
                dataframe["funding_latest"] = dataframe["funding_latest"].ffill().fillna(0.0)
            else:
                dataframe["funding_max"] = 0.0
                dataframe["funding_latest"] = 0.0
        else:
            dataframe["funding_max"] = 0.0
            dataframe["funding_latest"] = 0.0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # LONG: daily brake AND funding below gate threshold
        dataframe.loc[
            (dataframe["close"] > dataframe["sma200"])
            & (dataframe["adx"] > 25)
            & (dataframe["funding_max"] < self.FUNDING_MAX)
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EXIT: brake only. No funding exit — it cuts winners during the
        # hottest part of the bull (when funding is highest = best moves).
        # Audit 2026-07-28: funding exit at 0.05% made things worse (-17% vs
        # -10%) because it exits at the top of the trend, then re-enters and
        # pays funding again. The brake's SMA200 exit is the only exit.
        dataframe.loc[
            (dataframe["close"] < dataframe["sma200"])
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
