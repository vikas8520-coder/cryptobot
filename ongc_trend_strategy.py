#!/usr/bin/env python3
"""
ongc_trend_strategy.py — monthly 200DMA trend strategy for ONGC.

Designed for ONGC's profile as a cyclical PSU oil & gas stock (not a compounder):
  - Rides crude oil upswings (ONGC > 200DMA = bullish crude regime)
  - Exits when crude cycle turns (ONGC < 200DMA)
  - Monthly cadence = noise filter (worth +7 CAGR points vs daily checking)
  - No stop loss (stops convert LTCG-0% to STCG-20.8% at ₹9,656 capital)
  - No exit band (bands tested strictly worse with monthly cadence)

WHY (audit 2026-07-27): ONGC's current hourly SMA(20/50) strategy trades 13x/year
and loses 75% of gross alpha to friction + tax. The monthly 200DMA cuts trades to
<1/year and captures crude cycles. Backtest (2006-2025, after-tax): 5.88% CAGR
vs 3.35% for the old strategy, with lower drawdown than buy-and-hold (-46% vs -79%).

Key insight from Claude: ONGC is a price-taker, not a compounder. Its upside is
administratively capped (windfall tax, APM gas price ceilings) but downside isn't.
A trend brake is exactly what this asymmetry demands.
"""
import time
import zlib

import apex_strategy
import pandas as pd  # used to normalize yfinance timestamps

DEFAULTS = {
    "pair": "ONGC/INR",
    "ticker": "ONGC.NS",
    "interval": "1d",           # Daily bars (not hourly)
    "lookback_bars": 400,       # Daily bars to cache (>= 200 for SMA + margin)
    "sma_period": 200,          # 200-day SMA (the trend brake)
    "stale_secs": 259200,       # 3 days idle threshold (daily bars)
    "start_price": 250.0,       # ₹ level for synthetic fallback
    "dividend_rate": 6.75,      # ₹/share per ex-date (ONGC pays ~₹13.50/yr in 3 dates)
    "ex_dates": [],             # ["YYYY-MM-DD", ...] — read by the engine
    "enter_tag": "monthly_200dma",
    "exit_reason": "below_200dma",
    "max_hold_cycles": 0,       # No time-stop (ride the cycle)
}


def _cfg(config):
    out = dict(DEFAULTS)
    block = (config or {}).get("ongc_trend_strategy")
    if isinstance(block, dict):
        for k, v in block.items():
            if k in out and v is not None:
                out[k] = v
    return out


class ONGCTrendStrategy:
    """Monthly 200DMA trend strategy for ONGC — ride crude cycles, exit when trend breaks.

    Signal is evaluated once per month (on month-end close). The engine ticks every 5s
    but this strategy only changes position at month boundaries. Between months, it
    holds the current position.
    """

    def __init__(self, config=None):
        self.config = config or {}
        self.params = _cfg(config)
        self.pair = str(self.params["pair"])
        self.synthetic = bool(self.config.get("synthetic"))
        self._syn = apex_strategy.SyntheticStrategy({
            **self.config,
            "exchange": {"pair_whitelist": [self.pair]},
            "synthetic_strategy": {
                "seed": zlib.crc32(str(self.params["ticker"]).encode()),
                "start_price": float(self.params["start_price"]),
            },
        }) if self.synthetic else None
        self._closes = []
        self._dates = []             # parallel list of bar dates
        self._last_fetch = 0.0
        self._last_bar_ts = 0.0
        self._last_price = None
        self._last_month = None      # Track month for signal cadence
        self._current_signal = "FLAT" # Persist signal between month-ends
        if not self.synthetic:
            try:
                self.refresh()
            except Exception as e:
                print(f"ongc_trend_strategy: init refresh failed ({type(e).__name__}: {e}); "
                      f"keeping {len(self._closes)} cached closes", flush=True)

    def refresh(self):
        """Pull ONGC daily candles into the cache, keeping bar dates for month-end selection."""
        if self.synthetic:
            return True
        try:
            import yfinance as yf
            df = yf.Ticker(str(self.params["ticker"])).history(
                period="2y", interval=str(self.params["interval"]))
            closes = [float(x) for x in df["Close"].tolist() if x == x]
            if not closes:
                return False
            self._closes = closes[-int(self.params["lookback_bars"]):]
            self._last_price = self._closes[-1]
            # Keep the date index so _month_end_price() can pick the last bar of the previous month.
            # Convert pandas timestamps to tz-naive dates to avoid timezone arithmetic bugs.
            self._dates = [pd.Timestamp(x).tz_localize(None).date() for x in df.index[-len(self._closes):]]
            self._last_bar_ts = df.index[-1].timestamp()
            self._last_fetch = time.time()
            return True
        except Exception as e:
            print(f"ongc_trend_strategy: refresh failed ({type(e).__name__}: {e}); "
                  f"keeping {len(self._closes)} cached closes", flush=True)
            return False

    def price(self, cycle=0):
        if self.synthetic and self._syn:
            return self._syn.price(cycle)
        if self._last_price is not None:
            return float(self._last_price)
        return apex_strategy.SyntheticStrategy(
            {"synthetic_strategy": {"start_price": float(self.params["start_price"])}}).price(cycle)

    def dividend_per_share(self):
        try:
            return float(self.params["dividend_rate"])
        except (TypeError, ValueError):
            return 0.0

    def ex_dates(self):
        raw = self.params.get("ex_dates")
        return [str(d) for d in raw if d] if isinstance(raw, (list, tuple)) else []

    def _market_idle(self):
        if self.synthetic or not self._last_bar_ts:
            return False
        return (time.time() - self._last_bar_ts) > float(self.params["stale_secs"])

    def _sma200(self, up_to_idx=None):
        """200-day SMA. Returns None if insufficient data (fails closed).

        up_to_idx: if provided, compute SMA using only closes[:up_to_idx+1].
        Used by _month_end_price() to avoid including the new month when
        evaluating the previous month-end signal.
        """
        period = max(1, int(self.params["sma_period"]))
        if not self._closes:
            return None
        closes = self._closes[:up_to_idx + 1] if up_to_idx is not None else self._closes
        if len(closes) < period:
            return None
        return apex_strategy.sma(closes, period)

    def _month_end_price(self, now_ist):
        """Find the last close of the previous calendar month and its SMA.

        This fixes the qwen3-coder review bug: on first run/restart mid-month,
        using `self._last_price` would compare a mid-month close to the 200DMA.
        Instead, we search backward for the last bar from the previous month and
        compute the SMA using only data up to and including that bar.
        """
        if not self._dates or not self._closes:
            return None, None
        prev_year = now_ist.year
        prev_month = now_ist.month - 1
        if prev_month == 0:
            prev_month = 12
            prev_year -= 1

        # Find the last bar whose year-month equals the previous month.
        for i in range(len(self._dates) - 1, -1, -1):
            d = self._dates[i]
            if d.year == prev_year and d.month == prev_month:
                px = self._closes[i]
                if px <= 0:
                    return None, None
                sma = self._sma200(up_to_idx=i)
                if sma is None:
                    return None, None
                return px, sma
        return None, None

    def _evaluate_month_end(self):
        """Evaluate the trend signal at month-end. Returns 'LONG' or 'FLAT'."""
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)

        # Use the last close of the previous month (month-end signal price).
        px, sma = self._month_end_price(now_ist)
        if px is None:
            return "FLAT"    # fail closed — no month-end bar or insufficient data
        return "LONG" if px > sma else "FLAT"

    def signal(self, state):
        """Generate entry/exit signals. Only changes position at month boundaries.

        Between month-ends, persists the last signal. This is the key difference
        from the hourly strategy — the monthly cadence IS the noise filter.
        """
        if self.synthetic and self._syn:
            return self._syn.signal(state)

        opens = list((state or {}).get("open") or [])
        max_open = int((state or {}).get("max_open", 1))
        paused = bool((state or {}).get("paused", False))

        # Check if we've crossed into a new month (IST = NSE timezone)
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(IST)
        current_month = now.strftime("%Y-%m")

        if current_month != self._last_month:
            # New month — evaluate signal at previous month-end close.
            self._current_signal = self._evaluate_month_end()
            self._last_month = current_month

        target = self._current_signal

        # Exit logic: if target is FLAT and we're in position, exit
        if target == "FLAT":
            for t in opens:
                return {"action": "exit", "trade_id": t.get("trade_id"),
                        "exit_reason": str(self.params["exit_reason"])}

        # Entry logic: if target is LONG and we have room
        if paused:
            return {"action": "none", "reason": "paused"}
        if self._market_idle():
            return {"action": "none", "reason": "market_idle"}
        if len(opens) >= max_open:
            return {"action": "none", "reason": "max_open_trades"}

        if target == "LONG" and not opens:
            return {"action": "enter", "pair": self.pair, "side": "long",
                    "enter_tag": str(self.params["enter_tag"])}

        return {"action": "none", "reason": "no_signal"}


if __name__ == "__main__":
    import sys
    syn = "--real" not in sys.argv
    s = ONGCTrendStrategy({"synthetic": syn, "ongc_trend_strategy": {"pair": "ONGC/INR"}})
    if not syn:
        ok = s.refresh()
        sma = s._sma200()
        print(f"ongc_trend_strategy: real refresh ok={ok}, {len(s._closes)} closes, "
              f"last ₹{s.price():.2f}, SMA200 ₹{sma:.2f}" if sma else
              f"ongc_trend_strategy: real refresh ok={ok}, {len(s._closes)} closes, "
              f"last ₹{s.price():.2f}, SMA200=None")
        print(f"  signal: {s._evaluate_month_end()}")
    else:
        print(f"ongc_trend_strategy: synthetic mode, price sample ₹{s.price(0):.2f}")
    st = {"cycle": 12, "paused": False, "max_open": 1, "open": []}
    print(f"  dividend ₹{s.dividend_per_share():.2f}/share, ex-dates {s.ex_dates()}")
    print("  signal:", s.signal(st))
