"""Backtest-only ADX-sweep probe: TrendFollowLS2 with ADX_MIN fixed at 30. NOT for the live bot — offline pre-registered sweep only (audit 2026-07-23)."""
from TrendFollowLS2 import TrendFollowLS2


class TrendFollowLS2ADX30(TrendFollowLS2):
    ADX_MIN = 30   # fixed entry-bar (long & short share this gate); sweep only
