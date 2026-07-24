"""Backtest-only ADX-sweep probe: TrendFollowHopt with buy_adx fixed at 20 (optimize off). NOT for the live bot — offline pre-registered sweep only (audit 2026-07-23)."""
from TrendFollowHopt import TrendFollowHopt
from freqtrade.strategy import IntParameter


class TrendFollowHoptADX20(TrendFollowHopt):
    buy_adx = IntParameter(15, 35, default=20, space="buy", optimize=False)   # fixed entry-bar; sweep only
