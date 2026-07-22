#!/usr/bin/env python3
"""
div_strategy.py — signal source for the high-dividend Indian equity paper bots (ONGC, ITC).

Sibling of spx_strategy.py and deliberately shaped the same way: signal() is pure, the real
feed lives behind refresh(), and a seeded walk covers the offline test. One class drives BOTH
bots — the instrument is config["div_strategy"]["ticker"], exactly as spx_strategy became
instrument-agnostic for the SPY/NIFTYBEES pair. Two bots, one strategy, no dialect drift.

The one strategy difference from spx_strategy is WHY this bot exists. A dividend name is held
for the yield, and the way to lose that yield is to keep holding through a structural
downtrend — the payout is 5-7%/yr and a bad year in ONGC is -30%. So the SMA cross is gated
by a 200-day MA hold filter: cross up ALONE never opens, price must also be above its 200-DMA.
Same brake idea as the brakedhold bot, applied to an equity.

Design, in priority order:

  1. signal() STAYS PURE — same contract as spx_strategy.SpxStrategy.signal(state_dict). It
     reads the two candle caches plus the dict it is handed and NEVER fetches, so a slow
     yfinance call cannot stall the engine tick.
  2. TWO SEPARATE FEEDS, TWO SEPARATE CACHES. The cross is computed on hourly bars; the
     200-DMA needs 200 *daily* closes, which 10 days of hourly bars cannot produce. refresh()
     pulls both (period="10d"/1h and period="2y"/1d) and caches them independently, so a
     failed daily pull cannot silently degrade the hourly cross into a 200-bar "DMA".
  3. THE FILTER FAILS CLOSED. If the daily cache is too short to compute the DMA (cold boot,
     every daily fetch failed), _above_dma() returns False and the bot does NOT open. An
     under-warmed brake must block entries, never wave them through — the whole point of the
     filter is the entry it prevents.
  4. SYNTHETIC MODE IS DETERMINISTIC AND PER-TICKER. config["synthetic"] delegates to the
     seeded walk, with the seed derived from the ticker (crc32, NOT hash() — hash() is salted
     per process, so a hash()-seeded "deterministic" series replays differently every run).
     ONGC and ITC therefore get different-but-replayable series.

Dividend income is NOT modelled here. dividend_per_share() just reports the configured per-
share ₹ payout; the engine credits it as cash on the ex-date (see ongc_engine.accrue_dividends).
Keeping it out of the price P&L is deliberate — a dividend folded into close_profit_abs would
make the SMA strategy look profitable when it was the payout, not the alpha.

Failure classes written against:
  - a one-off network flake read as a real price move: refresh() keeps the LAST GOOD cache on
    a failed pull instead of blanking it (same rule as spx_strategy).
  - after-hours / NSE holiday: market_idle() reports on the newest hourly bar's own timestamp,
    so entries stop but exits do not — a position never gets stranded by the closing bell.
  - SMA/DMA computed on too little history: apex_strategy.sma returns None below the period
    (never a partial mean), so an under-warmed strategy emits "none", never a phantom cross.
"""
import time
import zlib

import apex_strategy   # reuse the proven seeded walk + sma() helper (single source)

DEFAULTS = {
    "pair": "ONGC/INR",
    "ticker": "ONGC.NS",       # yfinance symbol for the real feed (e.g. ONGC.NS, ITC.NS)
    "interval": "1h",          # yfinance interval for the trading (cross) feed
    "lookback_bars": 120,      # hourly candles kept in the cache (>= sma_slow + margin)
    "sma_fast": 20,            # SMA-cross fast period, in HOURLY bars
    "sma_slow": 50,            # SMA-cross slow period, in HOURLY bars
    "dma_filter": 200,         # hold filter period, in DAILY bars (the 200-DMA)
    "daily_bars": 400,         # daily closes kept in the cache (>= dma_filter + margin)
    "stale_secs": 5400,        # >90min since the newest hourly bar -> market considered idle
    "start_price": 250.0,      # ₹ level for the synthetic walk / cold-cache fallback only
    "dividend_rate": 0.0,      # ₹ per share paid on each ex-date (engine credits it as cash)
    "ex_dates": [],            # ["YYYY-MM-DD", ...] — read by the engine, not by signal()
    "enter_tag": "div_dma",
    "exit_reason": "sma_cross_down",
    "max_hold_cycles": 0,      # 0 = exit only on the down-cross; >0 = also time-stop
}


def _cfg(config):
    """Merge config["div_strategy"] over DEFAULTS. A missing/garbage block must degrade to
    the default, never crash the engine loop."""
    out = dict(DEFAULTS)
    block = (config or {}).get("div_strategy")
    if isinstance(block, dict):
        for k, v in block.items():
            if k in out and v is not None:
                out[k] = v
    return out


class DividendStrategy:
    """Real-feed SMA-cross gated by a 200-DMA hold filter, with a synthetic fallback. Same
    signal() contract as spx_strategy.SpxStrategy so the engine treats every paper bot
    identically."""

    def __init__(self, config=None):
        self.config = config or {}
        self.params = _cfg(config)
        self.pair = str(self.params["pair"])
        self.synthetic = bool(self.config.get("synthetic"))
        # Seed from the ticker so ONGC and ITC replay as different-but-stable series (rule 4).
        self._syn = apex_strategy.SyntheticStrategy({
            **self.config,
            "exchange": {"pair_whitelist": [self.pair]},
            "synthetic_strategy": {
                **((self.config.get("synthetic_strategy") or {}) if isinstance(
                    self.config.get("synthetic_strategy"), dict) else {}),
                "seed": zlib.crc32(str(self.params["ticker"]).encode()),
                "start_price": float(self.params["start_price"]),
            },
        }) if self.synthetic else None
        # Real-feed caches: hourly closes drive the cross, daily closes drive the 200-DMA.
        self._closes = []
        self._daily = []
        self._last_fetch = 0.0
        self._last_bar_ts = 0.0
        self._last_price = None

    # ---- real feed (called by the engine on its throttle, NEVER inside signal) ----

    def refresh(self):
        """Pull BOTH feeds into their caches. Returns True only when both legs succeeded.
        On any failure it keeps the last good cache (rule 2 in the header) and returns False
        — the engine logs it, the strategy keeps answering off whatever it last had."""
        if self.synthetic:
            return True
        ok_hourly = self._refresh_hourly()
        ok_daily = self._refresh_daily()
        return ok_hourly and ok_daily

    def _refresh_hourly(self):
        """Trading feed: 10 days of hourly bars for the SMA cross."""
        try:
            import yfinance as yf
            df = yf.Ticker(str(self.params["ticker"])).history(
                period="10d", interval=str(self.params["interval"]))
            closes = [float(x) for x in df["Close"].tolist() if x == x]  # drop NaN
            if not closes:
                return False
            self._closes = closes[-int(self.params["lookback_bars"]):]
            self._last_price = self._closes[-1]
            # newest bar's own timestamp -> lets market_idle() know the NSE has stopped ticking
            self._last_bar_ts = df.index[-1].timestamp()
            self._last_fetch = time.time()
            return True
        except Exception as e:
            print(f"div_strategy: hourly refresh failed for {self.params['ticker']} "
                  f"({type(e).__name__}: {e}); keeping {len(self._closes)} cached closes",
                  flush=True)
            return False

    def _refresh_daily(self):
        """Hold-filter feed: 2 years of daily bars, the only source long enough for a 200-DMA.
        Kept separate from the hourly pull so one failing leg never contaminates the other."""
        try:
            import yfinance as yf
            df = yf.Ticker(str(self.params["ticker"])).history(period="2y", interval="1d")
            closes = [float(x) for x in df["Close"].tolist() if x == x]
            if not closes:
                return False
            self._daily = closes[-int(self.params["daily_bars"]):]
            return True
        except Exception as e:
            print(f"div_strategy: daily refresh failed for {self.params['ticker']} "
                  f"({type(e).__name__}: {e}); keeping {len(self._daily)} cached daily closes",
                  flush=True)
            return False

    def price(self, cycle=0):
        """Latest hourly close. Falls back to the synthetic walk if the real cache is empty so
        the paper engine can still size/fill a trade on a cold start."""
        if self.synthetic and self._syn:
            return self._syn.price(cycle)
        if self._last_price is not None:
            return float(self._last_price)
        # cold real cache -> deterministic fallback so the chain never wedges flat
        return apex_strategy.SyntheticStrategy(
            {"synthetic_strategy": {"start_price": float(self.params["start_price"])}}).price(cycle)

    def dividend_per_share(self):
        """Configured ₹ payout per share on an ex-date. The ENGINE credits it as cash; this
        method only reports the rate, so the strategy stays a pure price-signal source."""
        try:
            return float(self.params["dividend_rate"])
        except (TypeError, ValueError):
            return 0.0

    def ex_dates(self):
        """Configured ex-dates as "YYYY-MM-DD" strings. Garbage entries are dropped rather
        than raised — a typo'd date must cost one missed credit, not the engine loop."""
        raw = self.params.get("ex_dates")
        return [str(d) for d in raw if d] if isinstance(raw, (list, tuple)) else []

    def market_idle(self):
        """True when the newest real hourly bar is older than stale_secs (after hours, NSE
        holiday, weekend). Idle blocks only NEW ENTRIES, never exits, so a position is never
        stranded when the close bell rings."""
        if self.synthetic or not self._last_bar_ts:
            return False
        return (time.time() - self._last_bar_ts) > float(self.params["stale_secs"])

    # ---- signal (pure: reads the two caches + the state dict only) ----

    def signal(self, state):
        """Same contract as spx_strategy: state is a plain dict, returns
        {"action": enter|exit|none, ...}. Synthetic mode delegates verbatim so the paper test
        is identical to the SPX/Nifty ones."""
        if self.synthetic and self._syn:
            return self._syn.signal(state)

        cycle = int((state or {}).get("cycle", 0))
        opens = list((state or {}).get("open") or [])
        max_open = int((state or {}).get("max_open", 1))
        paused = bool((state or {}).get("paused", False))
        hold = int(self.params["max_hold_cycles"])

        cross = self._cross()   # +1 up-cross, -1 down-cross, 0 none/insufficient

        # EXITS BEAT ENTRIES: close on a down-cross, or on the optional time-stop. Note the
        # 200-DMA filter is NOT consulted here — it gates getting in, never getting out.
        for t in opens:
            try:
                age = cycle - int(t.get("open_cycle", cycle))
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
        if self.market_idle():
            return {"action": "none", "reason": "market_idle"}
        if len(opens) >= max_open:
            return {"action": "none", "reason": "max_open_trades"}
        if cross > 0:
            # The yield is not worth owning a name below its 200-DMA — cross up alone is not
            # an entry (rule 3: an uncomputable DMA blocks, it does not wave through).
            if not self._above_dma():
                return {"action": "none", "reason": "below_200dma"}
            return {"action": "enter", "pair": self.pair, "side": "long",
                    "enter_tag": str(self.params["enter_tag"])}
        return {"action": "none", "reason": "no_cross"}

    def _above_dma(self):
        """True when the latest price sits above its 200-day MA. False when the daily cache is
        too short to answer — see rule 3; the filter fails CLOSED."""
        dma = apex_strategy.sma(self._daily, int(self.params["dma_filter"]))
        if dma is None:
            return False
        px = self._last_price if self._last_price is not None else self._daily[-1]
        return float(px) > float(dma)

    def _cross(self):
        """+1 if the fast SMA just crossed above the slow one, -1 if below, 0 otherwise or on
        insufficient history. Uses apex_strategy.sma (None below the period, never a partial
        mean), so an under-warmed cache cannot fake a cross."""
        c = self._closes
        fast, slow = int(self.params["sma_fast"]), int(self.params["sma_slow"])
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
    ticker = "ITC.NS" if "--itc" in sys.argv else "ONGC.NS"
    s = DividendStrategy({"synthetic": syn,
                          "div_strategy": {"ticker": ticker, "pair": ticker.split(".")[0] + "/INR"}})
    if not syn:
        ok = s.refresh()
        print(f"div_strategy: {ticker} real refresh ok={ok}, {len(s._closes)} hourly / "
              f"{len(s._daily)} daily closes, last ₹{s.price():.2f}, "
              f"above_200dma={s._above_dma()}, idle={s.market_idle()}")
    else:
        print(f"div_strategy: {ticker} synthetic mode, price sample ₹{s.price(0):.2f}")
    st = {"cycle": 12, "paused": False, "max_open": 1, "open": []}
    print(f"  dividend ₹{s.dividend_per_share():.2f}/share, ex-dates {s.ex_dates()}")
    print("  signal:", s.signal(st))
