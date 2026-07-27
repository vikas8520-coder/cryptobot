#!/usr/bin/env python3
"""
apex_strategy.py — the ApeX bot's signal source. PHASE P2: SYNTHETIC ONLY.

The other 5 bots inherit freqtrade's IStrategy and get their candles from ccxt. ApeX has
neither, so this module is the whole strategy layer — and in P2 it deliberately trades a
made-up price series instead of a real one.

WHY SYNTHETIC FIRST. The risky part of this bot is not the alpha, it is the plumbing:
signal -> paper fill -> sqlite row -> stats_lib -> dashboard/telegram. That chain has 9
consumers and one wrong column name makes /stats silently report zeros (APEX_PLAN 1.3).
A seeded random walk lets the entire chain be proven end-to-end, repeatably, with no
exchange, no `apexomni` in ./.venv, and no network flake to misread as a bug. real_signal()
below is the P3 shape, present but unused, so swapping the price source is a one-line
change in the engine rather than a rewrite here.

Rules, in priority order:

  1. PURE MATH, ZERO I/O. Nothing here reads a file, opens a socket, calls time.time(),
     or touches EngineState. signal() is a function of the dict it is handed, so the
     paper test can drive 500 cycles in a millisecond and get the same answer twice.
  2. DETERMINISTIC. The walk comes from random.Random(seed) with the seed in the config,
     so "the trade that lost money on cycle 240" is reproducible. A strategy whose
     backtest cannot be replayed is a strategy whose bug reports cannot be either.
  3. EXITS BEAT ENTRIES. When an open position is due to close and a new entry is also
     eligible on the same cycle, signal() returns the exit. Otherwise a max_open_trades
     ceiling could wedge the engine into never closing anything.

Failure classes this file is written against:
  - a synthetic series that only ever goes up, which would make the fill simulator and
    the P&L math look correct while hiding every sign error. The walk has zero drift by
    default and produces losers.
  - a strategy that emits an entry every single cycle: at a 5s throttle that is 720
    trades an hour of ledger noise. Entries are gated on entry_every_cycles.
  - "not used yet" code silently becoming used: real_signal() returns None for any input
    it cannot justify a signal on, and the engine never calls it in P2.
"""
import random

# Defaults for config["synthetic_strategy"]. Every one is overridable; these values give
# a ~2% hourly-candle vol series around 100k that trades roughly once a minute at a 5s
# throttle — brisk enough to exercise the chain in a paper test, not so brisk it floods.
DEFAULTS = {
    "seed": 20260722,          # config-pinned so a run is replayable (rule 2)
    "start_price": 98000.0,    # arbitrary BTC-ish level; only the RETURNS matter
    "vol": 0.004,              # per-candle stdev as a fraction of price
    "drift": 0.0,              # zero on purpose — a drifting walk hides sign errors
    "candles": 2000,           # series length; the engine wraps around it
    "entry_every_cycles": 12,  # one entry attempt per N cycles (12 * 5s = 1/min)
    "hold_cycles": 6,          # K: close a synthetic trade N cycles after it opened
    "exit_reason": "synthetic_hold_expired",
    "enter_tag": "synthetic_walk",
}

SMA_FAST = 10                  # real_signal only — P3
SMA_SLOW = 30


def _cfg(config):
    """Merge config["synthetic_strategy"] over DEFAULTS. Tolerates a missing/garbage
    block: a broken knob must degrade to the default, never crash the engine loop."""
    out = dict(DEFAULTS)
    block = (config or {}).get("synthetic_strategy")
    if isinstance(block, dict):
        for k, v in block.items():
            if k in out and v is not None:
                out[k] = v
    return out


def make_series(seed, start_price, vol, drift, candles):
    """A seeded geometric random walk as in-memory OHLCV.

    Returns a list of (index, open, high, low, close, volume). The index stands in for a
    timestamp on purpose — this series has no wall-clock meaning and pretending otherwise
    would invite someone to chart it against real time."""
    rng = random.Random(int(seed))
    out = []
    price = float(start_price)
    for i in range(int(candles)):
        o = price
        c = max(0.01, o * (1.0 + rng.gauss(float(drift), float(vol))))
        # Wicks straddle the body so high/low are never inside it (an invariant any real
        # OHLCV consumer assumes, and the cheapest one to get wrong).
        hi = max(o, c) * (1.0 + abs(rng.gauss(0.0, float(vol) / 2)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, float(vol) / 2)))
        out.append((i, o, hi, lo, c, rng.uniform(1.0, 100.0)))
        price = c
    return out


class SyntheticStrategy:
    """Builds one deterministic OHLCV series at construction and answers signal() off it.

    Construction is the only expensive call; signal() is O(1) list indexing, which is what
    lets the engine call it on every 5s cycle without thinking about cost."""

    def __init__(self, config=None):
        self.params = _cfg(config)
        p = self.params
        self.series = make_series(p["seed"], p["start_price"], p["vol"],
                                  p["drift"], p["candles"])
        self.pair = self._pick_pair(config)

    @staticmethod
    def _pick_pair(config):
        """First whitelisted pair, so the synthetic trade lands on a pair the ops layer
        already expects to see from this bot."""
        wl = ((config or {}).get("exchange") or {}).get("pair_whitelist") or []
        return str(wl[0]) if wl else "BTC/USDC"

    def price(self, cycle):
        """Close of the candle at `cycle`, wrapping at the end of the series. Wrapping
        (rather than freezing at the last close) keeps prices moving on a long-lived
        daemon — a frozen price makes every trade close at exactly -2 * fee, which looks
        like a working P&L path and tests nothing."""
        if not self.series:
            return float(self.params["start_price"])
        return float(self.series[int(cycle) % len(self.series)][4])

    def candles(self, cycle, lookback=200):
        """The last `lookback` candles as of `cycle` — the shape real_signal() will be
        handed in P3, so the swap is a price-source change, not an interface change."""
        n = len(self.series)
        if not n:
            return []
        end = (int(cycle) % n) + 1
        return self.series[max(0, end - int(lookback)):end]

    def signal(self, state):
        """Pure. `state` is a plain dict, NOT EngineState:

            {"cycle": int, "paused": bool, "max_open": int,
             "open": [{"trade_id": int, "open_cycle": int}, ...]}

        Returns one of:
            {"action": "exit",  "trade_id": int, "exit_reason": str}
            {"action": "enter", "pair": str, "side": "long", "enter_tag": str}
            {"action": "none",  "reason": str}
        """
        p = self.params
        cycle = int((state or {}).get("cycle", 0))
        opens = list((state or {}).get("open") or [])
        max_open = int((state or {}).get("max_open", 1))
        paused = bool((state or {}).get("paused", False))

        # RULE 3: an expiring position is settled before any new one is considered.
        for t in opens:
            try:
                age = cycle - int(t.get("open_cycle", cycle))
            except (TypeError, ValueError):
                continue                      # malformed entry: leave it to force_exit
            if age >= int(p["hold_cycles"]):
                return {"action": "exit", "trade_id": t.get("trade_id"),
                        "exit_reason": str(p["exit_reason"])}

        if paused:
            return {"action": "none", "reason": "paused"}
        if len(opens) >= max_open:
            return {"action": "none", "reason": "max_open_trades"}
        every = max(1, int(p["entry_every_cycles"]))
        if cycle > 0 and cycle % every == 0:
            return {"action": "enter", "pair": self.pair, "side": "long",
                    "enter_tag": str(p["enter_tag"])}
        return {"action": "none", "reason": "no_signal"}


def sma(values, period):
    """Simple moving average of the last `period` values, or None if there aren't enough.
    None, not a partial average — a 4-sample "30-period SMA" is a lie that crosses early."""
    period = int(period)
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / float(period)


def real_signal(klines, fast=SMA_FAST, slow=SMA_SLOW):
    """P3 SHAPE, NOT WIRED — SMA cross on real ApeX klines.

    `klines` is a list of OHLCV tuples/lists (close at index 4), i.e. what
    apex_client.get_market_data() returns once it is normalized. Returns "enter", "exit",
    or None. Nothing in P2 calls this; it exists so the P3 change is "call real_signal
    instead of SyntheticStrategy.signal", with the price source being the only new risk."""
    closes = []
    for k in klines or []:
        try:
            closes.append(float(k[4]))
        except (TypeError, ValueError, IndexError, KeyError):
            return None                       # a malformed feed is not a flat signal
    f_now, s_now = sma(closes, fast), sma(closes, slow)
    f_prev, s_prev = sma(closes[:-1], fast), sma(closes[:-1], slow)
    if None in (f_now, s_now, f_prev, s_prev):
        return None                           # not enough history to claim a cross
    if f_prev <= s_prev and f_now > s_now:
        return "enter"
    if f_prev >= s_prev and f_now < s_now:
        return "exit"
    return None


if __name__ == "__main__":
    s = SyntheticStrategy({"exchange": {"pair_whitelist": ["BTC/USDC"]}})
    lo = min(c[4] for c in s.series)
    hi = max(c[4] for c in s.series)
    print(f"apex_strategy: {len(s.series)} synthetic candles on {s.pair}, "
          f"close range {lo:.2f}..{hi:.2f}, seed {s.params['seed']}")
    st = {"cycle": 0, "paused": False, "max_open": 3, "open": []}
    for c in range(0, 40):
        st["cycle"] = c
        sig = s.signal(st)
        if sig["action"] != "none":
            print(f"  cycle {c:3d} price {s.price(c):10.2f} -> {sig}")
