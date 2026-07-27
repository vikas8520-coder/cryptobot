"""Backtest-only ADX-sweep probe: TrendFollowLS2 with ADX_MIN fixed at 20. Parent uses a plain int attr (ADX_MIN), not an IntParameter, so the override is a simple reassignment. NOT for the live bot — offline pre-registered sweep only (audit 2026-07-23)."""
from TrendFollowLS2 import TrendFollowLS2


class TrendFollowLS2ADX20(TrendFollowLS2):
    ADX_MIN = 20   # fixed entry-bar (long & short share this gate); sweep only
