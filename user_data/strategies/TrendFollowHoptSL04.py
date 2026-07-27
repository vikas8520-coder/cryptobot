"""Backtest-only A/B probe: TrendFollowHopt with a tighter -0.04 stoploss. NOT for the live bot — offline pre-registered test only (audit 2026-07-23)."""
from TrendFollowHopt import TrendFollowHopt


class TrendFollowHoptSL04(TrendFollowHopt):
    stoploss = -0.04   # candidate tighter stop vs baseline -0.08 (pre-registered A/B only)
