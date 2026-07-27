"""BrakedHoldV2 -- 200DMA braked-hold with vol-targeted sizing and a wider exit band.

Design brief
------------
BrakedHold.py proved the core edge: hold a coin while it's above its 200-day
moving average, step to cash the moment it falls below, and accept the ~9-20
trades/year that costs (see BrakedHold.py's own docstring). This file keeps
that exact brake and adds two changes the India after-tax research (Lane B
dossier, research/crypto-bot-profitability-research-SYNTHESIS.md) flagged as
+EV without reopening the door to the turnover BrakedHold was built to avoid:

  1. Vol-targeted position sizing (custom_stake_amount): full-notional BTC
     runs ~45-70% annualized vol, which is what forces bad discretionary exits
     during drawdowns. Target sigma ~20-30% (mid 25%) by scaling the stake
     down when realized vol (20-day rolling stdev of daily returns,
     annualized) runs hot. A 25% relative rebalance band stops the bot from
     re-sizing on every candle's vol noise -- it only moves stake when the
     target weight has drifted more than 25% from the weight it last actually
     used for that pair.

  2. A 2% exit band (close < SMA200 * 0.98, not close < SMA200): the raw
     200DMA cross whipsaws around the line during chop, and every whipsaw
     round-trip eats the SAME 30%-tax-no-loss-offset + 1% TDS hit that made
     hard stops a net negative for BrakedHold (see
     nifty_backtest/btc_faber_backtest.py, variant "+5% stop"). Widening the
     exit brake by 2% cuts the false-exit rate at the cost of a small delay
     on real trend breaks.

Priority order when signals conflict: (1) the exit brake (capital
preservation) beats (2) entry, beats (3) sizing -- position size never gates
whether we're in or out of the trade.

NO hard stop -- stoploss=-0.99 is a freqtrade-required floor, not a real risk
control. The after-tax backtest showed a hard stop on top of the brake
DESTROYS returns: every stop-out is a taxable, non-offsettable realized loss
on a trade the brake would otherwise have exited a few days later for less
total tax drag.
"""
from datetime import datetime

import numpy as np
import pandas as pd
import talib.abstract as ta
from pandas import DataFrame
from freqtrade.strategy import IStrategy


# ---- VOL-TARGET SIZING ----
# Pure helper functions (no self, no freqtrade types) so they're unit-testable
# without spinning up a strategy instance or the exchange.

VOL_WINDOW = 20        # trading days used for the realized-vol estimate
TARGET_VOL = 0.25       # mid-point of the 20-30% target band from the tax research
REBAL_BAND = 0.25       # only re-size when target weight drifts >25% (relative) from last
MIN_WEIGHT = 0.0
MAX_WEIGHT = 1.0        # spot, no leverage -- can never size above the full proposed stake


def realized_vol(returns, window=VOL_WINDOW):
    """Annualized realized vol from a Series/array of daily returns (last `window`
    bars). NaN if there isn't enough history yet -- caller treats "can't measure"
    as "can't size", not as "vol is high"."""
    r = pd.Series(returns).dropna()
    if len(r) < window:
        return float("nan")
    return float(r.tail(window).std() * np.sqrt(252))


def vol_target_weight(vol, target_vol=TARGET_VOL, floor=MIN_WEIGHT, cap=MAX_WEIGHT):
    """Fraction of the proposed stake to actually deploy so realized vol runs near
    target_vol: weight = target/realized, clipped to [floor, cap]. A NaN or
    non-positive vol (no history yet / a dead market) falls back to the cap --
    full size -- since we'd rather size normally than starve a real signal."""
    if vol != vol or vol <= 0:   # NaN check without importing math
        return cap
    return float(min(cap, max(floor, target_vol / vol)))


def should_rebalance(new_weight, last_weight, band=REBAL_BAND):
    """True if new_weight has drifted from last_weight by more than `band`
    (relative). last_weight=None means this pair has never been sized before,
    so it always sizes on the first entry."""
    if last_weight is None or last_weight <= 0:
        return True
    return abs(new_weight - last_weight) / last_weight > band


class BrakedHoldV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False

    # Hold while the trend holds: no ROI target, no stop -- the (banded) 200-day
    # brake is the ONLY exit. See docstring: stops re-introduce the tax-drag
    # turnover BrakedHold's research killed.
    minimal_roi = {}
    stoploss = -0.99              # freqtrade-required floor, not a real risk control
    trailing_stop = False

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 220    # need 200 for the SMA + buffer

    EXIT_BAND = 0.98              # 2% below SMA200 before we call it a real trend break

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._last_weight = {}    # pair -> last-applied vol-target weight (rebalance-band state)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)
        dataframe["daily_ret"] = dataframe["close"].pct_change()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # enter/stay long while price is above the 200-day trend brake
        dataframe.loc[dataframe["close"] > dataframe["sma200"], "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # step to cash only once price falls 2% below the brake (whipsaw filter)
        dataframe.loc[dataframe["close"] < dataframe["sma200"] * self.EXIT_BAND, "exit_long"] = 1
        return dataframe

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                             proposed_stake: float, min_stake, max_stake: float,
                             leverage: float, entry_tag, side: str, **kwargs) -> float:
        # defensive: dp is only wired up by the freqtrade runtime, never in unit tests
        # or early startup -- fall back to full size rather than blocking an entry.
        weight = MAX_WEIGHT
        try:
            dp = getattr(self, "dp", None)
            if dp is not None:
                dataframe, _ = dp.get_analyzed_dataframe(pair, self.timeframe)
                if dataframe is not None and len(dataframe):
                    vol = realized_vol(dataframe["daily_ret"])
                    weight = vol_target_weight(vol)
        except Exception:
            weight = MAX_WEIGHT

        last = self._last_weight.get(pair)
        if should_rebalance(weight, last):
            self._last_weight[pair] = weight
        else:
            weight = last          # inside the rebalance band -- keep the old sizing

        stake = proposed_stake * weight
        if min_stake is not None:
            stake = max(stake, min_stake)
        return min(stake, max_stake)
