"""Backtest-only ADX-sweep probe: TrendFollowLS2 with ADX_MIN fixed at 25 (== live baseline value; harness sanity dup). NOT for the live bot — offline pre-registered sweep only (audit 2026-07-23)."""
from TrendFollowLS2 import TrendFollowLS2


class TrendFollowLS2ADX25(TrendFollowLS2):
    ADX_MIN = 25   # == live baseline; should reproduce baseline
