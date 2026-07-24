"""Backtest-only ADX-sweep probe: TrendFollowLS2 with ADX_MIN fixed at 35. NOT for the live bot — offline pre-registered sweep only (audit 2026-07-23)."""
from TrendFollowLS2 import TrendFollowLS2


class TrendFollowLS2ADX35(TrendFollowLS2):
    ADX_MIN = 35   # fixed entry-bar (long & short share this gate); sweep only
