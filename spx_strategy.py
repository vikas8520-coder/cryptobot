#!/usr/bin/env python3
"""
spx_strategy.py — the S&P 500 paper bot's signal source.

Sibling of apex_strategy.py, but with one deliberate difference: this bot has a REAL
price source (yfinance, free, no key) as well as the seeded-synthetic one. The market
data risk here is the opposite of ApeX's — the feed exists and is easy to hit, so the
danger is not "no candles" but "stale/after-hours/rate-limited candles read as signal".

Design, in priority order:

  1. signal() STAYS PURE. Same contract as apex_strategy.SyntheticStrategy.signal:
     signal(state_dict) -> {"action": enter|exit|none, ...}. It reasons only about the
     dict it is handed plus the in-memory candle cache; it NEVER fetches inside signal().
     The engine calls refresh() on its own throttle to update the cache, so a slow/failed
     yfinance call can never stall or crash the strategy tick.
  2. REAL FEED IS CACHED + STALE-GUARDED. refresh() pulls SPY hourly candles and stores
     them with a fetch timestamp. price()/signal() read the cache; if the cache is empty
     (first tick, or every fetch has failed) they FALL BACK to the synthetic walk so the
     paper chain still exercises end-to-end rather than wedging flat.
  3. SYNTHETIC MODE IS DETERMINISTIC. When config["synthetic"] is true the bot ignores
     yfinance entirely and delegates to a seeded walk (same engine as apex_strategy), so
     spx_paper_test.py replays byte-for-byte with no network.

The instrument is SPY (the tradeable S&P 500 ETF), not ^GSPC (the index): an index has
no shares to paper-fill, an ETF does, so the paper P&L maps to something you could
actually have traded once live.

Failure classes written against:
  - after-hours / weekend: yfinance returns the last session's bars; refresh() records
    the bar's own timestamp so the engine can down-weight a market that hasn't ticked.
  - a one-off network flake read as a real price move: refresh() keeps the LAST GOOD
    cache on a failed pull instead of blanking it.
  - SMA cross computed on too little history: sma() returns None (not a partial mean),
    so an under-warmed strategy emits "none", never a phantom cross.
"""
import time

import apex_strategy   # reuse the proven seeded walk + sma() helper (single source)

SPY = "SPY"
DEFAULTS = {
    "pair": "SPY/USD",
    "ticker": "SPY",           # yfinance symbol for the real feed (e.g. SPY, NIFTYBEES.NS)
    "interval": "1h",          # yfinance interval for the real feed
    "lookback_bars": 60,       # candles to keep in the cache (>= slow SMA + margin)
    "fast": 10,                # SMA-cross fast period
    "slow": 30,                # SMA-cross slow period
    "stale_secs": 5400,        # >90min since the newest bar -> market considered idle
    "enter_tag": "sma_cross_up",
    "exit_reason": "sma_cross_down",
    "max_hold_cycles": 0,      # 0 = exit only on the down-cross; >0 = also time-stop
    "dma_filter": 0,            # 0 = no trend filter; >0 = SMA(window) on DAILY closes,
                             #   entry blocked when price <= that SMA (audit 2026-07-22:
                             #   BTC backtest showed +200-DMA cuts maxDD ~14pts at the
                             #   cost of half the trades — Sharpe 1.20->2.48. Default
                             #   OFF so SPX/Nifty stay pure SMA-cross.)
}


def _cfg(config):
    out = dict(DEFAULTS)
    block = (config or {}).get("spx_strategy")
    if isinstance(block, dict):
        for k, v in block.items():
            if k in out and v is not None:
                out[k] = v
    return out


class SpxStrategy:
    """Real-feed SMA-cross with a synthetic fallback. Same signal() contract as
    apex_strategy.SyntheticStrategy so the engine treats both identically."""

    def __init__(self, config=None):
        self.config = config or {}
        self.params = _cfg(config)
        self.pair = str(self.params["pair"])
        self.synthetic = bool(self.config.get("synthetic"))
        # In synthetic mode we own a deterministic walk and never touch the network.
        self._syn = apex_strategy.SyntheticStrategy(
            {**self.config, "exchange": {"pair_whitelist": [self.pair]}}
        ) if self.synthetic else None
        # Real-feed cache: list of closes (chronological) + newest-bar wall-clock.
        self._closes = []
        self._last_fetch = 0.0
        self._last_bar_ts = 0.0
        self._last_price = None
        # audit 2026-07-25 time-stop bug: max_hold_cycles was consumed against the
        # engine's ~5s tick counter, so 72 "cycles" = ~6 min of wall clock and the
        # time-stop fired inside the FIRST hourly bar (killed V1: -1.07%). The hold
        # must be measured in CANDLE BAR CLOSES, not engine ticks — this monotonic
        # counter increments once per NEW bar seen by refresh(), so max_hold_cycles=72
        # means 72 bars. bar_index() exposes it; the engine stamps/ages trades by it.
        self._bars = 0
        self._daily = []           # daily closes for the optional DMA trend filter
        # audit 2026-07-22: a cold REAL start left _last_price=None, so price()
        # fell back to the synthetic 550 walk (547.87) and force-seed / first
        # auto-entry sized off a fake price. Warm the real feed once at init
        # (real mode only — synthetic mode owns its deterministic walk).
        if not self.synthetic:
            try:
                self.refresh()
            except Exception as e:
                print(f"spx_strategy: init refresh failed ({type(e).__name__}: {e}); "
                      f"keeping {len(self._closes)} cached closes", flush=True)
    # ---- real feed (called by the engine on its throttle, NEVER inside signal) ----

    def refresh(self):
        """Pull SPY candles into the cache. Returns True on a good pull. On any failure
        it keeps the last good cache (rule 2) and returns False — the engine logs it but
        the strategy keeps answering off whatever it last had."""
        if self.synthetic:
            return True
        try:
            import yfinance as yf
            df = yf.Ticker(str(self.params["ticker"])).history(
                period="10d", interval=str(self.params["interval"]))
            closes = [float(x) for x in df["Close"].tolist() if x == x]  # drop NaN
            if not closes:
                return False
            self._closes = closes[-int(self.params["lookback_bars"]):]
            self._last_price = self._closes[-1]
            # newest bar's own timestamp -> lets signal() know if the market is idle,
            # and drives the bar-close counter the time-stop ages against (audit
            # 2026-07-25): only a strictly newer bar timestamp is a NEW candle close.
            new_bar_ts = df.index[-1].timestamp()
            if new_bar_ts > self._last_bar_ts:
                self._bars += 1
            self._last_bar_ts = new_bar_ts
            self._last_fetch = time.time()
            # Optional DMA trend filter: pull DAILY closes only when configured
            # (audit 2026-07-22: keeps SPX/Nifty free of an extra yfinance call).
            dma = int(self.params.get("dma_filter", 0) or 0)
            if dma > 0:
                try:
                    d = yf.Ticker(str(self.params["ticker"])).history(
                        period="2y", interval="1d")
                    self._daily = [float(x) for x in d["Close"].tolist() if x == x]
                except Exception:
                    pass   # DMA filter simply stays un-fired if the daily pull fails
            return True
        except Exception as e:
            print(f"spx_strategy: refresh failed ({type(e).__name__}: {e}); "
                  f"keeping {len(self._closes)} cached closes", flush=True)
            return False

    def bar_index(self):
        """Monotonic count of candle bar closes seen so far (audit 2026-07-25).
        The engine stamps each trade's open_bar with this and ages the time-stop
        against it, so max_hold_cycles is measured in BARS, not ~5s engine ticks.
        Synthetic mode has no real bars; fall back to the walk's cycle so tests
        still exercise the time-stop path deterministically."""
        return int(self._bars)

    def price(self, cycle=0):
        """Latest close. Falls back to the synthetic walk if the real cache is empty so
        the paper engine can still size/fill a trade on a cold start."""
        if self.synthetic and self._syn:
            return self._syn.price(cycle)
        if self._last_price is not None:
            return float(self._last_price)
        # cold real cache -> deterministic fallback so the chain never wedges flat
        return apex_strategy.SyntheticStrategy(
            {"synthetic_strategy": {"start_price": 550.0}}).price(cycle)

    def _above_dma(self):
        """True when price is above the optional SMA(dma_filter) on DAILY closes.
        Returns True when the filter is OFF (dma_filter<=0) so default bots are
        unaffected. audit 2026-07-22: BTC backtest showed +200-DMA cuts
        maxDD ~14pts (Sharpe 1.20->2.48); SPX/Nifty keep dma_filter=0."""
        dma = int(self.params.get("dma_filter", 0) or 0)
        if dma <= 0:
            return True
        if len(self._daily) < dma + 1:
            return True   # not enough daily history yet — don't block on a cold start
        m = apex_strategy.sma(self._daily, dma)
        if m is None:
            return True
        return (self._last_price or 0.0) > m

    def _market_idle(self):
        """True when the newest real bar is older than stale_secs (after hours / weekend).
        Idle doesn't stop exits — only new entries, so a position never gets stranded when
        the close bell rings."""
        if self.synthetic or not self._last_bar_ts:
            return False
        return (time.time() - self._last_bar_ts) > float(self.params["stale_secs"])

    # ---- signal (pure: reads cache + state dict only) ----

    def signal(self, state):
        """Same contract as apex_strategy: state is a plain dict, returns
        {"action": enter|exit|none, ...}. Synthetic mode delegates verbatim so tests are
        identical to the ApeX paper test."""
        if self.synthetic and self._syn:
            return self._syn.signal(state)

        cycle = int((state or {}).get("cycle", 0))
        opens = list((state or {}).get("open") or [])
        max_open = int((state or {}).get("max_open", 1))
        paused = bool((state or {}).get("paused", False))
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        hold = int(self.params["max_hold_cycles"])
        now_bar = self.bar_index()   # audit 2026-07-25: age the time-stop by BARS

        cross = self._cross()   # +1 up-cross, -1 down-cross, 0 none/insufficient

        # RULE 3 (exits beat entries): close on a down-cross, or on the optional time-stop.
        for t in opens:
            # audit 2026-07-25: age by candle bar closes (open_bar), NOT engine ticks.
            # Falls back to open_cycle only for legacy rows opened before this fix.
            ob = t.get("open_bar")
            try:
                age = now_bar - int(ob) if ob is not None else cycle - int(t.get("open_cycle", cycle))
            except (TypeError, ValueError):
                continue
            if cross < 0:
                return {"action": "exit", "trade_id": t.get("trade_id"),
                        "exit_reason": str(self.params["exit_reason"])}
            if hold > 0 and age >= hold:
                return {"action": "exit", "trade_id": t.get("trade_id"),
                        "exit_reason": "time_stop"}

        if paused:
            return {"action": "none", "reason": "paused"}
        if self._market_idle():
            return {"action": "none", "reason": "market_idle"}
        if not self._above_dma():
            return {"action": "none", "reason": "below_dma"}
        if len(opens) >= max_open:
            return {"action": "none", "reason": "max_open_trades"}
        if cross > 0:
            return {"action": "enter", "pair": self.pair, "side": "long",
                    "enter_tag": str(self.params["enter_tag"])}
        return {"action": "none", "reason": "no_cross"}

    def _cross(self):
        """+1 if fast SMA just crossed above slow, -1 if below, 0 otherwise/insufficient.
        Uses apex_strategy.sma (returns None below the period, never a partial mean)."""
        c = self._closes
        fast, slow = int(self.params["fast"]), int(self.params["slow"])
        if len(c) < slow + 1:
            return 0
        f_now, s_now = apex_strategy.sma(c, fast), apex_strategy.sma(c, slow)
        f_prev, s_prev = apex_strategy.sma(c[:-1], fast), apex_strategy.sma(c[:-1], slow)
        if None in (f_now, s_now, f_prev, s_prev):
            return 0
        if f_prev <= s_prev and f_now > s_now:
            return 1
        if f_prev >= s_prev and f_now < s_now:
            return -1
        return 0


if __name__ == "__main__":
    import sys
    syn = "--real" not in sys.argv
    s = SpxStrategy({"synthetic": syn, "spx_strategy": {"pair": "SPY/USD"}})
    if not syn:
        ok = s.refresh()
        print(f"spx_strategy: real refresh ok={ok}, {len(s._closes)} closes, "
              f"last price {s.price():.2f}, idle={s._market_idle()}")
    else:
        print(f"spx_strategy: synthetic mode, price sample {s.price(0):.2f}")
    st = {"cycle": 12, "paused": False, "max_open": 1, "open": []}
    print("  signal:", s.signal(st))
