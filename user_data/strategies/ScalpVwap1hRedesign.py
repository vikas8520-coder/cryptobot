# ScalpVwap1hRedesign — PROTOTYPE. Not wired to any bot/plist. Do not deploy.
#
# WHY THIS EXISTS (scalp diagnosis 2026-07-23): ScalpVwap5m loses -65.94% at
# PF 0.54 even with fees forced to ZERO. That rules out "fees ate it" — the LOGIC
# is net-negative. The diagnosed root cause is reward:risk, not the thesis:
#   - target = snap back to VWAP  -> ~0.4% median gain
#   - stop   = fixed -2%          -> ~5x the target
#   so a ~0.2 R:R needs an ~83% hit rate just to break even, and 5m crypto
#   mean-reversion doesn't clear that because it is fighting intraday momentum.
#
# The three changes this prototype makes, each aimed at one term of that equation:
#   (1) 5m -> 1h. A 2sigma band on 1h is several times wider than on 5m, so the
#       edge per trade is large relative to cost instead of comparable to it, and
#       1h reverts more than 5m (less momentum-dominated).
#   (2) Exit at vwap + 1sigma, NOT at vwap. The old exit gave back the entire
#       upper half of the reversion. Entering at -2sigma and exiting at +1sigma
#       is a 3sigma target instead of a 2sigma one.
#   (3) Stop = 1.5 * ATR(14) instead of a flat -2%. A fixed percent stop is either
#       noise-tight or hilariously wide depending on regime; ATR makes the risk
#       leg scale with the same volatility that sets the reward leg, which is the
#       only way R:R stays constant across 2019 and 2022.
# Together: reward ~3sigma against risk ~1.5*ATR, instead of 2sigma against a
# fixed 2%. If this still shows PF < 1.1 then VWAP mean-reversion is dead on
# crypto at every timeframe and the family should be dropped, not re-tuned.
#
# NOTE ON CONFIG PRECEDENCE: freqtrade lets config_*.json override strategy
# attributes, and config_scalp.json pins stoploss -0.02 — which would silently
# reinstate the exact fixed stop this redesign exists to remove. Backtest via
#   -c config_scalp.json -c user_data/prototype_overlay_scalp.json
# so the ATR stop is actually the binding one.
#
# Standalone IStrategy on purpose (does NOT subclass ScalpVwap5m): the live
# strategy must stay free to change without silently mutating this prototype,
# and its merge_informative_pair("1h") is meaningless once the base tf IS 1h.

import numpy as np
from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta


class ScalpVwap1hRedesign(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False

    # Time-decay backstop only. The real exits are the +1sigma target and the ATR
    # stop; ROI just guarantees a stalled fade flattens inside ~3 days rather than
    # squatting on the single max_open_trades slot for weeks.
    minimal_roi = {"0": 0.05, "1440": 0.02, "2880": 0.01, "4320": 0.0}
    stoploss = -0.99                 # disabled on purpose -> custom_stoploss governs
    trailing_stop = False
    use_custom_stoploss = True

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count = 220       # EMA100 + ATR14 + session warmup on 1h

    band = DecimalParameter(1.0, 3.0, default=2.0, decimals=1, space="buy", optimize=True)
    adx_max = IntParameter(15, 40, default=25, space="buy", optimize=True)
    vol_mult = DecimalParameter(0.8, 2.0, default=1.0, decimals=1, space="buy", optimize=True)
    exit_band = DecimalParameter(0.0, 2.0, default=1.0, decimals=1, space="sell", optimize=True)
    atr_mult = DecimalParameter(1.0, 3.0, default=1.5, decimals=1, space="sell", optimize=True)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        df = dataframe
        day = df["date"].dt.floor("D")

        # --- session-anchored VWAP + expanding deviation band (00:00 UTC reset) ---
        # Expanding (never rolling-forward) => backward-only => no lookahead.
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        cum_vol = df.groupby(day)["volume"].cumsum()
        cum_tpv = (tp * df["volume"]).groupby(day).cumsum()
        df["vwap"] = cum_tpv / cum_vol.replace(0, np.nan)
        dev = df["close"] - df["vwap"]
        # min_periods=8, not the 12 used on 5m: a 1h session is only 24 candles, so
        # 12 would blind the strategy to half of every day. 8 still gives a usable
        # sigma while leaving two thirds of the session tradeable.
        df["vwap_sd"] = dev.groupby(day).transform(
            lambda s: s.expanding(min_periods=8).std())

        df["adx"] = ta.ADX(df, timeperiod=14)
        df["atr"] = ta.ATR(df, timeperiod=14)
        df["vol_ma"] = df["volume"].rolling(20).mean()

        # Trend filter is now NATIVE (base tf == 1h), so there is no informative
        # merge and therefore no not-yet-closed-candle leak to reason about.
        df["ema50"] = ta.EMA(df, timeperiod=50)
        df["ema100"] = ta.EMA(df, timeperiod=100)
        return df

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up_1h = (dataframe["close"] > dataframe["ema100"]) & \
                (dataframe["ema50"] > dataframe["ema100"])
        lower = dataframe["vwap"] - self.band.value * dataframe["vwap_sd"]
        dataframe.loc[
            (
                (dataframe["close"] < lower)                     # >Nsigma below fair value
                & (dataframe["adx"] < self.adx_max.value)        # range regime only
                & up_1h                                          # with the higher tide
                & (dataframe["volume"] > self.vol_mult.value * dataframe["vol_ma"])
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # The fix for the 0.2 R:R: ride PAST the mean to +1sigma instead of
        # bailing at vwap, which is where the old version donated its edge.
        upper = dataframe["vwap"] + self.exit_band.value * dataframe["vwap_sd"]
        dataframe.loc[
            (dataframe["close"] >= upper) & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe

    def custom_stoploss(self, pair: str, trade, current_time, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs):
        """Vol-scaled stop: entry - 1.5*ATR(14) as sampled at the entry candle.

        Anchored to the ENTRY candle's ATR, not the live one, so the risk leg is
        the one that was actually accepted when the trade was taken; freqtrade
        only ever ratchets a stop tighter, so this cannot silently widen.
        """
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None                      # keep whatever stop is already set
        entry_rows = df.loc[df["date"] <= trade.open_date_utc]
        if entry_rows.empty:
            return None
        atr = entry_rows.iloc[-1]["atr"]
        if atr is None or not np.isfinite(atr) or atr <= 0:
            return None
        stop_price = trade.open_rate - self.atr_mult.value * atr
        rel = (stop_price / current_rate) - 1.0
        # Clamp: a positive rel means price is already through the stop -> park the
        # stop just under price so it fires immediately instead of being rejected.
        return max(-0.99, min(rel, -0.0001))
