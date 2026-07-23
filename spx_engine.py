#!/usr/bin/env python3
"""
spx_engine.py — the S&P 500 paper bot daemon. PHASE: PAPER (real feed or synthetic).

Sibling of apex_engine.py. Same architecture — one in-memory EngineState written only by
the loop thread, read via snapshot() by the apex_api shim on a daemon thread — but two
deliberate differences:

  1. REAL PRICE SOURCE. Where ApeX P2 trades a synthetic walk, this bot trades REAL SPY
     candles (yfinance) via spx_strategy.SpxStrategy. The loop calls strategy.refresh()
     on a slow cadence (feed_refresh_secs, default 300s) so a slow/failed network pull
     happens on the engine thread's own schedule and can never stall a request handler
     or the fill logic. Synthetic mode (config["synthetic"]=true) is kept for the
     deterministic paper test.
  2. IT REUSES apex_api VERBATIM. apex_api.create_app is fully config-driven (port, auth,
     exchange label, trading_mode all come from config), so the S&P bot serves the exact
     same freqtrade dialect on :8086 without a second copy of the shim. One shim, two
     bots — no dialect drift between them.

Everything else — dry_run refusal, single-writer state, atomic heartbeat, boot
reconciliation of open rows, pessimistic both-sides-fee paper fills — is the ApeX P2
design, because that chain is already proven end-to-end against 9 ops-layer consumers.

Why PAPER only, and staying that way: Vikas is US-based now but relocating to India
(Aug 2026), where live US-equities trading through a retail broker is materially harder.
So this bot has NO order path to a broker at all — it refuses to start unless dry_run is
true, exactly like the ApeX engine. Going live is a separate, deliberate build with a
broker (Alpaca/IBKR) and real keys, not a config flip here.
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import apex_api            # config-driven freqtrade-dialect shim, reused as-is
import spx_store
import spx_strategy
from state_io import save_json

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE, "config_spx.json")
STATE_FILE = os.path.join(BASE, "spx_state.json")    # git-ignored by the *_state.json rule

DEFAULT_THROTTLE = 5       # seconds between strategy ticks
HEARTBEAT_EVERY = 300      # seconds between log lines
DEFAULT_FEED_REFRESH = 300  # seconds between real-feed pulls (yfinance is not per-tick)


def _row_to_trade(row):
    """Project a sqlite row into the in-memory trade shape /status serves (boot restore)."""
    return {
        "trade_id": row["id"], "id": row["id"], "pair": row.get("pair"),
        "base_currency": row.get("base_currency"), "quote_currency": "USD",
        "is_open": bool(row.get("is_open")), "is_short": bool(row.get("is_short")),
        "exchange": row.get("exchange", "spx"), "strategy": row.get("strategy"),
        "enter_tag": row.get("enter_tag"), "timeframe": row.get("timeframe", 60),
        "trading_mode": row.get("trading_mode", "spot"),
        "leverage": float(row.get("leverage", 1.0)),
        "amount": float(row.get("amount", 0.0)), "stake_amount": float(row.get("stake_amount", 0.0)),
        "open_rate": float(row.get("open_rate", 0.0)),
        "open_date": row.get("open_date"), "open_cycle": 0,
        "current_rate": float(row.get("open_rate", 0.0)), "profit_ratio": 0.0,
        "profit_pct": 0.0, "profit_abs": 0.0, "fee_open_cost": row.get("fee_open_cost"),
        "fee_close_cost": None, "funding_fees": 0.0, "orders": [],
    }


def _merge(base, over):
    """Recursive dict merge, later wins — so config_spx.secret.json can slot
    api_server.password into the tracked config without the password landing in git."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    for extra in cfg.get("add_config_files") or []:
        with open(os.path.join(here, extra)) as f:
            _merge(cfg, json.load(f))
    return cfg


class EngineState:
    """Single in-memory source of truth. Written by the engine thread, read via snapshot()
    by the uvicorn thread. Mirrors apex_engine.EngineState; the strategy is SpxStrategy so
    price()/signal() come from the real SPY feed (or the synthetic walk in test mode)."""

    def __init__(self, config):
        self.config = config
        self.starting_capital = float(config.get("dry_run_wallet", 1000))
        self.balance = self.starting_capital
        self.positions = []
        self.closed = []
        self.paused = False
        self.started_at = time.time()
        self.cycles = 0
        self.last_cycle = 0.0
        self._lock = threading.RLock()
        self.strategy = spx_strategy.SpxStrategy(config)
        self.next_trade_id = spx_store.max_trade_id() + 1
        for row in spx_store.load_open_trades():
            try:
                self.positions.append(_row_to_trade(row))
            except Exception as e:
                print(f"spx_engine: skip unrecoverable open row {row.get('id')}: {e}", flush=True)

    def snapshot(self):
        with self._lock:
            return {
                "balance": float(self.balance),
                "starting_capital": float(self.starting_capital),
                "positions": list(self.positions),
                "closed": list(self.closed),
                "paused": bool(self.paused),
                "dry_run": bool(self.config.get("dry_run", True)),
                "started_at": float(self.started_at),
                "cycles": int(self.cycles),
                "last_cycle": float(self.last_cycle),
            }

    def set_paused(self, value):
        with self._lock:
            self.paused = bool(value)

    def force_exit(self, tradeid):
        """Close tradeid ("all" for everything); returns count closed. Single choke point
        for closing — apex_api forwards the id here, same as the ApeX engine."""
        with self._lock:
            if tradeid == "all":
                targets = list(self.positions)
            else:
                targets = [t for t in self.positions if str(t.get("trade_id")) == str(tradeid)]
            closed = 0
            for t in targets:
                price = self.strategy.price(self.cycles)
                if self._close(t, price, exit_reason="force_exit"):
                    closed += 1
            return closed

    def _open(self, pair, side="long", enter_tag=None):
        """Translate an ENTER signal into one paper trade. Sized from config stake_amount,
        priced off the strategy's current price, pessimistic both-sides fee."""
        fee = float(self.config.get("fee", 0.0005))
        price = self.strategy.price(self.cycles)
        stake = float(self.config.get("stake_amount", self.starting_capital))
        amount = (stake * (1.0 - fee)) / price
        trade_id = int(self.next_trade_id)
        self.next_trade_id += 1
        base = str(pair.split("/")[0])
        trade = {
            "trade_id": trade_id, "id": trade_id, "pair": pair,
            "base_currency": base, "quote_currency": "USD",
            "is_open": True, "is_short": (side == "short"),
            "exchange": "spx", "strategy": "SpxSmaCross", "enter_tag": enter_tag,
            "timeframe": 60, "trading_mode": "spot", "leverage": 1.0,
            "amount": amount, "stake_amount": stake,
            "open_rate": price, "open_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "open_timestamp": int(time.time() * 1000), "open_cycle": self.cycles,
            "current_rate": price, "profit_ratio": 0.0, "profit_pct": 0.0,
            "profit_abs": 0.0, "fee_open_cost": stake * fee,
            "fee_close_cost": None, "funding_fees": 0.0, "orders": [],
        }
        trade_id = spx_store.open_trade(trade)
        trade["id"] = trade_id
        trade["trade_id"] = trade_id
        self.positions.append(trade)
        return trade

    def _close(self, trade, price, exit_reason="sma_cross_down"):
        """Flatten one open paper trade, book P&L (price move minus BOTH fees), write the
        CLOSED ledger row. Returns True if a position was actually closed."""
        if not trade.get("is_open"):
            return False
        fee = float(self.config.get("fee", 0.0005))
        open_rate = float(trade.get("open_rate", price))
        direction = -1.0 if trade.get("is_short") else 1.0
        gross = direction * (price - open_rate) / open_rate
        profit_ratio = gross - 2.0 * fee
        profit_abs = trade.get("stake_amount", 0.0) * profit_ratio
        trade.update({
            "is_open": False, "close_rate": price, "current_rate": price,
            "close_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "exit_reason": exit_reason, "profit_ratio": profit_ratio,
            "profit_pct": profit_ratio * 100.0, "profit_abs": profit_abs,
            "fee_close_cost": trade.get("stake_amount", 0.0) * fee, "funding_fees": 0.0,
        })
        ok = spx_store.close_trade(
            trade["id"], price, close_profit=profit_ratio,
            close_profit_abs=profit_abs, exit_reason=exit_reason,
            fee_close_cost=trade["stake_amount"] * fee)
        if ok:
            with self._lock:
                self.positions = [t for t in self.positions if t.get("id") != trade["id"]]
                self.closed.append(trade)
            self.balance = self.starting_capital + sum(t.get("profit_abs", 0.0) for t in self.closed)
        return ok


    def _check_stops(self):
        """Hard stop-loss per open position. Reads config["stoploss"] (e.g. -0.05).
        Long-only: if current price is down more than |stoploss| from entry, flatten.
        WHY: engine used to exit only on SMA-cross/time-stop, so adverse moves with no
        cross could bleed until signal. Caps downside regardless of strategy.
        audit 2026-07-23 money-bleed pass.
        """
        sl = float(self.config.get("stoploss", -0.05))
        if sl >= 0:
            return  # positive/zero stoploss means disabled
        if not self.positions:
            return
        price = self.strategy.price(self.cycles) if self.strategy else None
        if price is None:
            return
        with self._lock:
            for t in list(self.positions):
                open_rate = float(t.get("open_rate") or 0.0)
                if open_rate <= 0:
                    continue
                loss_ratio = (price - open_rate) / open_rate  # long-only
                if loss_ratio <= sl:
                    self._close(t, price, exit_reason="stoploss")

    def cycle_strategy(self):
        """One strategy tick: build the plain-dict state, act on the signal."""
        with self._lock:
            open_state = [{"trade_id": t["trade_id"], "open_cycle": t.get("open_cycle", self.cycles)}
                          for t in self.positions]
            sig = self.strategy.signal({
                "cycle": self.cycles, "paused": self.paused,
                "max_open": int(self.config.get("max_open_trades", 1)),
                "open": open_state,
            })
        action = (sig or {}).get("action")
        if action == "enter":
            self._open(sig.get("pair", "SPY/USD"),
                       side=sig.get("side", "long"), enter_tag=sig.get("enter_tag"))
        elif action == "exit":
            with self._lock:
                target = next((t for t in self.positions if t.get("trade_id") == sig.get("trade_id")), None)
            if target:
                price = self.strategy.price(self.cycles)
                self._close(target, price, exit_reason=sig.get("exit_reason", "exit_signal"))

    def note_cycle(self):
        with self._lock:
            self.cycles += 1
            self.last_cycle = time.time()


def heartbeat_file(state):
    snap = state.snapshot()
    save_json(STATE_FILE, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": "paper-synthetic" if state.config.get("synthetic") else "paper-real",
        "paper_trades": len(state.closed),
        "open_trades": len(snap["positions"]),
        "paused": snap["paused"],
        "cycles": snap["cycles"],
        "balance": snap["balance"],
    }, indent=2)


def main():
    ap = argparse.ArgumentParser(description="S&P 500 paper bot daemon")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"spx_engine: cannot load {args.config}: {type(e).__name__}: {e}", flush=True)
        return 1

    # No broker order path exists in this file; a live config would mean unmanaged capital.
    if not cfg.get("dry_run", True):
        print("spx_engine: REFUSING to start — dry_run is false and the paper engine has "
              "no broker order path. Going live is a separate build (Alpaca/IBKR), not a "
              "config flip.", flush=True)
        return 1

    state = EngineState(cfg)
    throttle = float((cfg.get("internals") or {}).get("process_throttle_secs", DEFAULT_THROTTLE))
    feed_every = float(cfg.get("feed_refresh_secs", DEFAULT_FEED_REFRESH))
    port = int((cfg.get("api_server") or {}).get("listen_port", 8086))

    # Warm the real feed once before serving, so the first /status isn't cold.
    state.strategy.refresh()

    try:
        apex_api.serve_in_thread(state)
    except Exception as e:
        print(f"spx_engine: REST shim failed to start on :{port}: {type(e).__name__}: {e}", flush=True)
        return 1

    mode = "PAPER (synthetic)" if cfg.get("synthetic") else "PAPER (real SPY feed)"
    print(f"spx_engine: up — dry_run wallet ${state.balance:.2f}, shim on :{port}, "
          f"throttle {throttle}s, feed refresh {feed_every}s, {mode}", flush=True)

    last_beat = 0.0
    last_feed = 0.0
    while True:
        try:
            state.note_cycle()
            now = time.time()
            # Real-feed pull on its own slow cadence (skipped in synthetic mode by refresh()).
            if now - last_feed >= feed_every:
                last_feed = now
                if not cfg.get("synthetic"):
                    state.strategy.refresh()
            state.cycle_strategy()
            state._check_stops()            # hard stop-loss (audit 2026-07-23)
            if now - last_beat >= HEARTBEAT_EVERY:
                last_beat = now
                snap = state.snapshot()
                print(f"spx_engine: heartbeat cycles={snap['cycles']} "
                      f"balance={snap['balance']:.2f} open={len(snap['positions'])} "
                      f"paused={snap['paused']}", flush=True)
                heartbeat_file(state)
        except Exception as e:
            print(f"spx_engine: cycle error: {type(e).__name__}: {e}", flush=True)
        time.sleep(throttle)


if __name__ == "__main__":
    sys.exit(main() or 0)
