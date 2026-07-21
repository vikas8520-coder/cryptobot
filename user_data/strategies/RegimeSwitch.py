# RegimeSwitch — the capstone. ONE bot that detects the market regime and
# switches behavior, instead of being locked into a single philosophy.
#
# The lesson from the gauntlet: trend-following owns BULL markets but gets
# chopped up in bears; mean-reversion survives bears but misses bulls. So the
# obvious move is: use the RIGHT tool for the CURRENT regime.
#
# REGIME DETECTION — BTC is the tide that moves all crypto boats:
#   bull regime  = BTC close > BTC EMA200  AND  BTC EMA50 > BTC EMA200
#   (every coin consults BTC's trend via an "informative pair", so the whole
#    portfolio switches together based on the broad market, not each coin alone.)
#
# BEHAVIOR PER REGIME:
#   BULL  -> trend-following entries (buy strength, ride winners) — TrendFollow logic
#   BEAR  -> GO TO CASH: take no new entries, and force-exit any open trade when
#            the regime flips down. In our tests, NOT trading beat every bearish
#            strategy (cash 0% > mean-reversion's small losses).
#
# The bet: capture TrendFollow's bull upside (+7.6%) while removing its bear
# downside (-0.7%) by simply sitting out when BTC isn't trending up.
#
# (The bear branch is deliberately "cash". You could instead plug mean-reversion
#  dip-buys in there — left as the obvious next experiment.)

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta


class RegimeSwitch(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    minimal_roi = {"0": 10}          # ROI disabled — let winners run
    stoploss = -0.10                 # wide stop, trends need room
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count = 200       # BTC EMA200 needs history

    def informative_pairs(self):
        # Load BTC at our timeframe so every pair can read the market regime.
        return [("BTC/USDT", self.timeframe)]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- this pair's own trend indicators (for the bull/trend-follow branch) ---
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema_trend"] = ta.EMA(dataframe, timeperiod=100)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        # --- market regime from BTC (the tide) ---
        if self.dp:
            btc = self.dp.get_pair_dataframe("BTC/USDT", self.timeframe)
            btc = btc.copy()
            btc["btc_ema50"] = ta.EMA(btc, timeperiod=50)
            btc["btc_ema200"] = ta.EMA(btc, timeperiod=200)
            # 1 = bull regime, 0 = not-bull (bear/chop)
            btc["btc_bull"] = (
                (btc["close"] > btc["btc_ema200"])
                & (btc["btc_ema50"] > btc["btc_ema200"])
            ).astype(int)
            dataframe = merge_informative_pair(
                dataframe, btc[["date", "btc_bull"]],
                self.timeframe, self.timeframe, ffill=True,
            )
        else:
            dataframe["btc_bull_1h"] = 0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        bull = dataframe["btc_bull_1h"] == 1

        # BULL regime -> trend-following (buy strength). BEAR -> no entry (cash).
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
        # Exit on trend break OR when the market regime flips to bear (get to cash).
        dataframe.loc[
            (
                (
                    (dataframe["ema_fast"] < dataframe["ema_slow"])
                    | (dataframe["close"] < dataframe["ema_slow"])
                    | (dataframe["btc_bull_1h"] == 0)
                )
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1
        return dataframe
