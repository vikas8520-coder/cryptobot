# RegimeSwitchV2 — RegimeSwitch + a HYSTERESIS filter on the regime signal.
#
# V1's failure: the raw regime (BTC vs EMA200) flip-flopped during bear-market
# relief rallies — BTC popped above EMA200 for a few candles, the bot flashed
# "BULL", bought the bounce, got chopped, then flipped back and force-exited into
# weakness. The lagging signal whipsawed exactly at the transitions.
#
# THE FIX — hysteresis (a sticky signal that resists flip-flopping):
#   - Flip to BULL only after 24 consecutive bull candles (a full day). Slow and
#     demanding, so brief bear-market bounces no longer trip it into bull traps.
#   - Flip to BEAR after just 6 consecutive bear candles. Fast, so capital gets
#     defensive quickly. (Asymmetry: slow to greed, fast to fear.)
#   - Between those thresholds, the regime HOLDS its previous state instead of
#     toggling every candle.
#
# Everything else matches V1: bull -> trend-follow, confirmed-bear -> cash.

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import numpy as np
import talib.abstract as ta


class RegimeSwitchV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    minimal_roi = {"0": 10}
    stoploss = -0.10
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 230  # EMA200 + 24-candle hysteresis window

    # Hysteresis thresholds (candles of confirmation required to flip).
    BULL_CONFIRM = 24   # slow to turn bullish — dodge bull traps
    BEAR_CONFIRM = 6    # fast to turn defensive — protect capital

    def informative_pairs(self):
        return [("BTC/USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT", self.timeframe).copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)

            # raw regime: 1 = bull-ish right now, 0 = not
            raw = (
                (btc["close"] > btc["btc_ema200"])
                & (btc["btc_ema50"] > btc["btc_ema200"])
            ).astype(int)

            # --- hysteresis: only flip on sustained confirmation ---
            # bull flip: the last BULL_CONFIRM candles were ALL bull
            bull_flip = raw.rolling(self.BULL_CONFIRM).min().eq(1)
            # bear flip: the last BEAR_CONFIRM candles were ALL bear
            bear_flip = raw.rolling(self.BEAR_CONFIRM).max().eq(0)

            regime = np.where(bull_flip, 1.0, np.where(bear_flip, 0.0, np.nan))
            # hold previous state between flips; start defensive (cash) until proven
            btc["btc_regime"] = DataFrame({"r": regime})["r"].ffill().fillna(0).values

            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_regime"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            dataframe["btc_regime_1h"] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bull = dataframe["btc_regime_1h"] == 1
        dataframe.loc[
            (
                bull
                & (dataframe["close"] > dataframe["ema_fast"])
                & (dataframe["ema_fast"] > dataframe["ema_slow"])
                & (dataframe["ema_slow"] > dataframe["ema_trend"])
                & (dataframe["adx"] > 20)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (
                    (dataframe["ema_fast"] < dataframe["ema_slow"])
                    | (dataframe["close"] < dataframe["ema_slow"])
                    | (dataframe["btc_regime_1h"] == 0)  # now stable, not flip-floppy
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
