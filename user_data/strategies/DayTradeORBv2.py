# DayTradeORBv2 — repair attempt on DayTradeORB (port 8084), NOT deployed.
#
# WHY THIS FILE EXISTS (diagnosis 2026-07-28): DayTradeORB backtested -64.95% over
# 2024-08..2026-07 (2093 trades, 40.2% win). The exit-reason table named three
# mechanical failures, each fixed below:
#
#   1. INVERTED PAYOFF. trailing_stop_positive=0.01/offset=0.02 capped the average
#      winner at +1.59% while stoploss=-0.03 let the average loser run to -3.19%.
#      A 1:2 reward:risk needs a 67% hit rate to break even; the strategy hit 40%.
#      FIX: drop the trailing stop, set an explicit target >= 2x the stop.
#   2. SESSION-CLOSE CHURN. The 23:00 UTC force-flat produced 1164 of 2093 exits
#      (56%) at -0.30% avg and only 24% winners — a bucket that lost 353 USDT while
#      paying ~233 USDT of fees to do it. FIX: hold to the target/stop, time-exit at
#      24h instead of at a wall-clock hour that has no relationship to the entry.
#   3. OVERTRADING. The opening range was ONE 00:00 candle, median width 0.52-0.87%
#      — barely 3x the 0.20% round-trip fee. Price closed above it ~9 bars/day on
#      ~78% of days, and nothing stopped re-entry, so 7553 raw signals fired.
#      FIX: 4h opening range (median width 1.08-1.95%) + one entry per pair per day.
#
# Also adds the only long-side filter that measured positive in a 49-coin sweep:
# volume expansion vs the 20-bar mean (+0.08% at 24h vs -0.01% unfiltered).
#
# HONEST EXPECTATION: these fixes repair the GEOMETRY, not the EDGE. An event study
# over 49 coins x 4.5y found the long ORB's forward return is ~0.00% at every range
# length (1/2/4/6h) and horizon (4/8/12/24h) — a precisely-measured zero, versus a
# 0.20% spot toll. This file should bleed slower, not win. See the report.

from datetime import datetime, timedelta

from freqtrade.strategy import IStrategy, DecimalParameter, IntParameter
from pandas import DataFrame
import talib.abstract as ta


class DayTradeORBv2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    # 1:2 reward:risk — the inversion fix. Target must clear the stop by 2x so a
    # sub-50% hit rate can still net positive after the 0.20% round trip.
    minimal_roi = {"0": 0.05}
    stoploss = -0.025
    trailing_stop = False            # was the winner-capping mechanism; removed

    MAX_HOLD_HOURS = 24              # replaces the 23:00 UTC wall-clock flush

    process_only_new_candles = True
    startup_candle_count = 220

    or_hours = IntParameter(2, 6, default=4, space="buy", optimize=True)
    vol_mult = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="buy",
                                optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")
        hour = df["date"].dt.hour
        orh = self.or_hours.value

        # Opening range = high/low across the first `orh` candles of the UTC day.
        # Aggregated over the OR window only, then broadcast to the whole day; the
        # entry gate below requires hour >= orh, so every OR candle is closed first.
        in_or = hour < orh
        or_agg = df[in_or].groupby(day[in_or]).agg(_hi=("high", "max"), _lo=("low", "min"))
        df["or_high"] = day.map(or_agg["_hi"])
        df["or_low"] = day.map(or_agg["_lo"])
        df["past_or"] = hour >= orh

        df["ema_trend"] = ta.EMA(df, timeperiod=200)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # One entry per pair per day: only the FIRST bar that clears the range counts.
        # Without this the 2093-trade churn returns.
        broke = df["past_or"] & df["or_high"].notna() & (df["close"] > df["or_high"])
        df["first_break"] = broke & (broke.groupby(day).cumsum() == 1)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                dataframe["first_break"]
                & (dataframe["close"] > dataframe["ema_trend"])   # uptrend gate
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                                                                  # expansion filter
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(self, pair: str, trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """Time-stop measured from ENTRY, not from a wall-clock hour."""
        if current_time - trade.open_date_utc >= timedelta(hours=self.MAX_HOLD_HOURS):
            return "max_hold"
        return None
