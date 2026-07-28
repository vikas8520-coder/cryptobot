#!/usr/bin/env python3
"""
nifty_engine.py — the Nifty 50 paper bot daemon. PHASE: PAPER (real feed or synthetic).

Sibling of spx_engine.py, and deliberately near-identical: same single-writer EngineState,
same reuse of the config-driven apex_api shim (here on :8087), same pessimistic both-sides-
fee paper fills, same dry_run refusal. It reuses spx_strategy.SpxStrategy VERBATIM — that
strategy became instrument-agnostic (config["spx_strategy"]["ticker"]), so this bot just
points it at NIFTYBEES.NS instead of SPY. One strategy, two markets, no dialect drift.

The only real differences from spx_engine: the ledger (nifty_store), the currency (INR),
the exchange label ("nifty"), and the instrument (the tradeable Nifty 50 ETF, NIFTYBEES on
the NSE). NIFTYBEES is the ETF an Indian broker actually lets you trade — so this paper bot
maps to the market Vikas can go live on after relocating to India, unlike the SPY bot.

PAPER only, same as SPX: no broker order path exists in this file; it refuses to start
unless dry_run is true. Going live is a separate build (Zerodha/Upstox + keys), not a flip.
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import apex_api            # config-driven freqtrade-dialect shim, reused as-is
import nifty_store
import ltcg_strategy     # tax-efficient long-term swing trading strategy
from state_io import save_json

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE, "config_nifty.json")
STATE_FILE = os.path.join(BASE, "nifty_state.json")   # git-ignored by the *_state.json rule

DEFAULT_THROTTLE = 5
HEARTBEAT_EVERY = 300
DEFAULT_FEED_REFRESH = 300


def _row_to_trade(row):
    """Project a sqlite row into the in-memory trade shape /status serves (boot restore)."""
    return {
        "trade_id": row["id"], "id": row["id"], "pair": row.get("pair"),
        "base_currency": row.get("base_currency"), "quote_currency": "INR",
        "is_open": bool(row.get("is_open")), "is_short": bool(row.get("is_short")),
        "exchange": row.get("exchange", "nifty"), "strategy": row.get("strategy"),
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
    """Recursive dict merge, later wins — so config_nifty.secret.json can slot
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
    by the uvicorn thread. Mirrors spx_engine.EngineState; strategy is SpxStrategy pointed
    at NIFTYBEES.NS via config."""

    def __init__(self, config):
        self.config = config
        self.starting_capital = float(config.get("dry_run_wallet", 100000))
        self.balance = self.starting_capital
        self.positions = []
        self.closed = []
        self.paused = False
        self.started_at = time.time()
        self.cycles = 0
        self.last_cycle = 0.0
        self._lock = threading.RLock()
        # Use tax-efficient LTCG strategy for long-term swing trading
        self.strategy = ltcg_strategy.LTCGStrategy(config)
        self.next_trade_id = nifty_store.max_trade_id() + 1
        for row in nifty_store.load_open_trades():
            try:
                self.positions.append(_row_to_trade(row))
            except Exception as e:
                print(f"nifty_engine: skip unrecoverable open row {row.get('id')}: {e}", flush=True)

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
        """Close tradeid ("all" for everything); returns count closed. Single choke point."""
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
            "base_currency": base, "quote_currency": "INR",
            "is_open": True, "is_short": (side == "short"),
            "exchange": "nifty", "strategy": "NiftySmaCross", "enter_tag": enter_tag,
            "timeframe": 60, "trading_mode": "spot", "leverage": 1.0,
            "amount": amount, "stake_amount": stake,
            "open_rate": price, "open_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "open_timestamp": int(time.time() * 1000), "open_cycle": self.cycles,
            # audit 2026-07-25: stamp the candle-bar index at entry so the time-stop
            # ages in BARS, not ~5s engine ticks (the old open_cycle bug).
            "open_bar": self.strategy.bar_index() if hasattr(self.strategy, "bar_index") else 0,
            "current_rate": price, "profit_ratio": 0.0, "profit_pct": 0.0,
            "profit_abs": 0.0, "fee_open_cost": stake * fee,
            "fee_close_cost": None, "funding_fees": 0.0, "orders": [],
        }
        trade_id = nifty_store.open_trade(trade)
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
        ok = nifty_store.close_trade(
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
            open_state = [{"trade_id": t["trade_id"], "open_cycle": t.get("open_cycle", self.cycles),
                           "open_bar": t.get("open_bar")}
                          for t in self.positions]
            sig = self.strategy.signal({
                "cycle": self.cycles, "paused": self.paused,
                "max_open": int(self.config.get("max_open_trades", 1)),
                "open": open_state,
            })
        action = (sig or {}).get("action")
        if action == "enter":
            self._open(sig.get("pair", "NIFTYBEES/INR"),
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
    ap = argparse.ArgumentParser(description="Nifty 50 paper bot daemon")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"nifty_engine: cannot load {args.config}: {type(e).__name__}: {e}", flush=True)
        return 1

    # No broker order path exists in this file; a live config would mean unmanaged capital.
    if not cfg.get("dry_run", True):
        print("nifty_engine: REFUSING to start — dry_run is false and the paper engine has "
              "no broker order path. Going live is a separate build (Zerodha/Upstox), not a "
              "config flip.", flush=True)
        return 1

    state = EngineState(cfg)
    throttle = float((cfg.get("internals") or {}).get("process_throttle_secs", DEFAULT_THROTTLE))
    feed_every = float(cfg.get("feed_refresh_secs", DEFAULT_FEED_REFRESH))
    port = int((cfg.get("api_server") or {}).get("listen_port", 8087))

    # Warm the real feed once before serving, so the first /status isn't cold.
    state.strategy.refresh()

    try:
        apex_api.serve_in_thread(state)
    except Exception as e:
        print(f"nifty_engine: REST shim failed to start on :{port}: {type(e).__name__}: {e}", flush=True)
        return 1

    mode = "PAPER (synthetic)" if cfg.get("synthetic") else "PAPER (real NIFTYBEES feed)"
    print(f"nifty_engine: up — dry_run wallet ₹{state.balance:.2f}, shim on :{port}, "
          f"throttle {throttle}s, feed refresh {feed_every}s, {mode}", flush=True)

    last_beat = 0.0
    last_feed = 0.0
    while True:
        try:
            state.note_cycle()
            now = time.time()
            if now - last_feed >= feed_every:
                last_feed = now
                if not cfg.get("synthetic"):
                    state.strategy.refresh()
            state.cycle_strategy()
            state._check_stops()            # hard stop-loss (audit 2026-07-23)
            if now - last_beat >= HEARTBEAT_EVERY:
                last_beat = now
                snap = state.snapshot()
                print(f"nifty_engine: heartbeat cycles={snap['cycles']} "
                      f"balance={snap['balance']:.2f} open={len(snap['positions'])} "
                      f"paused={snap['paused']}", flush=True)
                heartbeat_file(state)
        except Exception as e:
            print(f"nifty_engine: cycle error: {type(e).__name__}: {e}", flush=True)
        time.sleep(throttle)


if __name__ == "__main__":
    sys.exit(main() or 0)
