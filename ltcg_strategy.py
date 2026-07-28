#!/usr/bin/env python3
"""
ltcg_strategy.py — tax-efficient long-term swing trading strategy for NIFTYBEES.

Designed to maximize after-tax returns by targeting LTCG treatment (>12 month holding periods).
Entry: 50/200 SMA golden cross (slow, high-conviction signals)
Exit: Close < 200DMA for 5+ consecutive days (reduces whipsaw)
No hard stop-loss (200DMA acts as the exit mechanism)
Target: 1-2 trades per year with 18-24 month holding periods

WHY (audit 2026-07-27): Indian tax rules make LTCG (13%) far more efficient than STCG (20.8%).
The 366-day cliff is the single biggest lever — one extra day of holding is worth ~1 point of CAGR.
This strategy deliberately sacrifices trade frequency for tax efficiency.
"""
import time
import apex_strategy

DEFAULTS = {
    "pair": "NIFTYBEES/INR",
    "ticker": "NIFTYBEES.NS",
    "interval": "1d",          # Daily timeframe for long-term signals
    "lookback_bars": 250,      # Enough for 200DMA + margin
    "fast": 50,                # Golden cross fast period
    "slow": 200,               # Golden cross slow period
    "stale_secs": 86400,       # 1 day idle threshold
    "enter_tag": "golden_cross",
    "exit_reason": "dma_breakdown",
    "max_hold_cycles": 0,      # No time-stop — let the 200DMA decide
    "dma_filter": 0,            # 200DMA is the exit signal, not entry filter
    "breakdown_days": 5,        # Days below 200DMA before exit (reduces whipsaw)
}


def _cfg(config):
    out = dict(DEFAULTS)
    block = (config or {}).get("ltcg_strategy")
    if isinstance(block, dict):
        for k, v in block.items():
            if k in out and v is not None:
                out[k] = v
    return out


class LTCGStrategy:
    """Tax-efficient long-term swing trading strategy targeting LTCG treatment."""

    def __init__(self, config=None):
        self.config = config or {}
        self.params = _cfg(config)
        self.pair = str(self.params["pair"])
        self.synthetic = bool(self.config.get("synthetic"))
        self._syn = apex_strategy.SyntheticStrategy(
            {**self.config, "exchange": {"pair_whitelist": [self.pair]}}
        ) if self.synthetic else None
        self._closes = []
        self._last_fetch = 0.0
        self._last_bar_ts = 0.0
        self._last_price = None
        self._bars = 0
        self._below_dma_count = 0    # Consecutive days below 200DMA
        if not self.synthetic:
            try:
                self.refresh()
            except Exception as e:
                print(f"ltcg_strategy: init refresh failed ({type(e).__name__}: {e}); "
                      f"keeping {len(self._closes)} cached closes", flush=True)

    def refresh(self):
        """Pull NIFTYBEES daily candles into the cache."""
        if self.synthetic:
            return True
        try:
            import yfinance as yf
            df = yf.Ticker(str(self.params["ticker"])).history(
                period="1y", interval=str(self.params["interval"]))
            closes = [float(x) for x in df["Close"].tolist() if x == x]
            if not closes:
                return False
            self._closes = closes[-int(self.params["lookback_bars"]):]
            self._last_price = self._closes[-1]
            new_bar_ts = df.index[-1].timestamp()
            if new_bar_ts > self._last_bar_ts:
                self._bars += 1
            self._last_bar_ts = new_bar_ts
            self._last_fetch = time.time()
            return True
        except Exception as e:
            print(f"ltcg_strategy: refresh failed ({type(e).__name__}: {e}); "
                  f"keeping {len(self._closes)} cached closes", flush=True)
            return False

    def bar_index(self):
        """Monotonic count of candle bar closes seen so far."""
        return int(self._bars)

    def price(self, cycle=0):
        """Latest close. Falls back to synthetic walk if cache is empty."""
        if self.synthetic and self._syn:
            return self._syn.price(cycle)
        if self._last_price is not None:
            return float(self._last_price)
        return apex_strategy.SyntheticStrategy(
            {"synthetic_strategy": {"start_price": 100.0}}).price(cycle)

    def _market_idle(self):
        """True when the newest bar is older than stale_secs."""
        if self.synthetic or not self._last_bar_ts:
            return False
        return (time.time() - self._last_bar_ts) > float(self.params["stale_secs"])

    def _dma(self):
        """200-day moving average."""
        if len(self._closes) < 200:
            return None
        return apex_strategy.sma(self._closes, 200)

    def _golden_cross(self):
        """+1 if 50 SMA just crossed above 200 SMA, -1 if below, 0 otherwise."""
        c = self._closes
        if len(c) < 200 + 1:
            return 0
        f_now, s_now = apex_strategy.sma(c, 50), apex_strategy.sma(c, 200)
        f_prev, s_prev = apex_strategy.sma(c[:-1], 50), apex_strategy.sma(c[:-1], 200)
        if None in (f_now, s_now, f_prev, s_prev):
            return 0
        if f_prev <= s_prev and f_now > s_now:
            return 1
        if f_prev >= s_prev and f_now < s_now:
            return -1
        return 0

    def signal(self, state):
        """Generate entry/exit signals targeting LTCG treatment."""
        if self.synthetic and self._syn:
            return self._syn.signal(state)

        cycle = int((state or {}).get("cycle", 0))
        opens = list((state or {}).get("open") or [])
        max_open = int((state or {}).get("max_open", 1))
        paused = bool((state or {}).get("paused", False))
        breakdown_days = int(self.params.get("breakdown_days", 5))
        now_bar = self.bar_index()

        dma = self._dma()
        golden_cross = self._golden_cross()

        # Exit logic: breakdown below 200DMA for consecutive days
        for t in opens:
            if dma is not None and self._last_price < dma:
                self._below_dma_count += 1
            else:
                self._below_dma_count = 0

            if self._below_dma_count >= breakdown_days:
                return {"action": "exit", "trade_id": t.get("trade_id"),
                        "exit_reason": str(self.params["exit_reason"])}

        # Entry logic: golden cross only
        if paused:
            return {"action": "none", "reason": "paused"}
        if self._market_idle():
            return {"action": "none", "reason": "market_idle"}
        if len(opens) >= max_open:
            return {"action": "none", "reason": "max_open_trades"}
        if golden_cross > 0:
            return {"action": "enter", "pair": self.pair, "side": "long",
                    "enter_tag": str(self.params["enter_tag"])}
        return {"action": "none", "reason": "no_cross"}


if __name__ == "__main__":
    import sys
    syn = "--real" not in sys.argv
    s = LTCGStrategy({"synthetic": syn, "ltcg_strategy": {"pair": "NIFTYBEES/INR"}})
    if not syn:
        ok = s.refresh()
        print(f"ltcg_strategy: real refresh ok={ok}, {len(s._closes)} closes, "
              f"last price {s.price():.2f}, idle={s._market_idle()}")
    else:
        print(f"ltcg_strategy: synthetic mode, price sample {s.price(0):.2f}")
    st = {"cycle": 12, "paused": False, "max_open": 1, "open": []}
    print("  signal:", s.signal(st))
