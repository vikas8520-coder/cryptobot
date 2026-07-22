#!/usr/bin/env python3
"""
apex_engine.py — the ApeX Omni bot daemon. PHASE P1: FLAT FOREVER.

ApeX is a DEX, not a Freqtrade/ccxt exchange, so this bot is a standalone process
instead of a `config_*.json` + strategy pair like the other 5 (APEX_PLAN, PATH A).
P1 builds only the half that has to be right before any money logic exists: the
process, its state object, and the REST surface the ops layer polls.

What this process does, in priority order:

  1. Owns the ONE in-memory EngineState. The loop thread is its single writer;
     apex_api only ever reads a snapshot() copy. Same single-writer discipline the
     guardian uses for guard_state.json.
  2. Hosts apex_api on :8085 on a daemon thread, so the shim cannot outlive the state
     it describes — a surviving shim would report a stale flat book to the guardian.
  3. Heartbeats on the config's internals.process_throttle_secs, catching everything:
     print / sleep / continue. A launchd KeepAlive restart loop is a worse failure than
     a logged exception, so the daemon never dies of its own accord.

What this process deliberately does NOT do in P1: import apexomni, open a socket to
ApeX, hold a position, or place an order. It holds ZERO positions by construction —
there is no order path in this file to go wrong. `apexomni` pins setuptools<81 and
pulls web3, and installing it would put the 5 running freqtrade bots (pinned
ccxt 4.5.65) at risk, so the SDK stays out of ./.venv until the P0 spike clears it.

Failure classes this file is written against:
  - dry_run flipped by accident: P1 refuses to start unless dry_run is true, because a
    live config here would silently mean "unmanaged capital" — there is no engine yet.
  - a crash in the loop killing the REST surface: the loop catches everything, and the
    shim runs on its own thread with its own state copy.
  - state written from two places: nothing outside this module mutates EngineState
    except the three documented POST verbs, each a single guarded setter.
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import apex_api
import apex_store
import apex_strategy
from state_io import save_json

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE, "config_apex.json")
STATE_FILE = os.path.join(BASE, "apex_state.json")   # git-ignored by the *_state.json rule

DEFAULT_THROTTLE = 5      # seconds; mirrors config.json:77 internals.process_throttle_secs
HEARTBEAT_EVERY = 300     # seconds between log lines — bot_apex.log is rotated, not infinite


def _row_to_trade(row):
    """Project a sqlite row (dict) into the in-memory trade shape /status serves.
    Boot reconciliation only restores what the engine needs to keep trading and to
    report an honest open book — the full ledger history stays in the sqlite file."""
    return {
        "trade_id": row["id"], "id": row["id"], "pair": row.get("pair"),
        "base_currency": row.get("base_currency"), "quote_currency": "USDC",
        "is_open": bool(row.get("is_open")), "is_short": bool(row.get("is_short")),
        "exchange": row.get("exchange", "apex"), "strategy": row.get("strategy"),
        "enter_tag": row.get("enter_tag"), "timeframe": row.get("timeframe", 60),
        "trading_mode": row.get("trading_mode", "futures"),
        "leverage": float(row.get("leverage", 1.0)),
        "amount": float(row.get("amount", 0.0)), "stake_amount": float(row.get("stake_amount", 0.0)),
        "open_rate": float(row.get("open_rate", 0.0)),
        "open_date": row.get("open_date"), "open_cycle": 0,
        "current_rate": float(row.get("open_rate", 0.0)), "profit_ratio": 0.0,
        "profit_pct": 0.0, "profit_abs": 0.0, "fee_open_cost": row.get("fee_open_cost"),
        "fee_close_cost": None, "funding_fees": 0.0, "orders": [],
    }
def _merge(base, over):
    """Recursive dict merge, later wins — the same precedence freqtrade gives
    add_config_files, so config_apex.secret.json can slot api_server.password into the
    tracked config without the password ever landing in git."""
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path):
    """Load the config plus every add_config_files overlay, relative to the config's dir.
    A missing/broken overlay is fatal on purpose: booting with a blank api_server.password
    would put an unauthenticated shim on :8085."""
    with open(path) as f:
        cfg = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    for extra in cfg.get("add_config_files") or []:
        with open(os.path.join(here, extra)) as f:
            _merge(cfg, json.load(f))
    return cfg


class EngineState:
    """The single in-memory source of truth. Written by the engine thread, read (via
    snapshot()) by the uvicorn thread — APEX_PLAN 4.5 requires the shim to be a pure
    memory read, so nothing here ever does I/O.

    P1 invariant: positions and closed are ALWAYS empty. The lists exist so apex_api
    serves the right *shape* today and needs no change when P2 fills them.
    """

    def __init__(self, config):
        self.config = config
        self.starting_capital = float(config.get("dry_run_wallet", 1000))
        self.balance = self.starting_capital     # simulated wallet, seeded per config
        self.positions = []                      # open trades, freqtrade field names (APEX_PLAN 1.2)
        self.closed = []                         # closed trades, same field names
        self.paused = False                      # /stopentry -> True, /reload_config -> False
        self.started_at = time.time()
        self.cycles = 0
        self.last_cycle = 0.0
        self._lock = threading.RLock()
        # P2: paper fill simulator. Restores the ledger id so /forceexit and downstream
        # consumers keep referring to the id the sqlite row actually owns.
        self.synthetic = bool((config.get("synthetic")))
        self.strategy = apex_strategy.SyntheticStrategy(config) if self.synthetic else None
        self.next_trade_id = apex_store.max_trade_id() + 1
        # Boot reconciliation (APEX_PLAN 4.7): a KeepAlive restart must not report a flat
        # book while positions are live. Restore any open rows the ledger still carries.
        for row in apex_store.load_open_trades():
            try:
                self.positions.append(_row_to_trade(row))
            except Exception as e:
                print(f"apex_engine: skip unrecoverable open row {row.get('id')}: {e}", flush=True)

    def snapshot(self):
        """A shallow copy taken under the lock. Handlers must never hold a reference to
        the live lists — a mid-serialization mutation would emit a torn /status."""
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
        """Idempotent — the guardian re-POSTs stopentry every 15s while tripped
        (portfolio_guard.py:137-141)."""
        with self._lock:
            self.paused = bool(value)

    def force_exit(self, tradeid):
        """Close `tradeid` ("all" for everything); returns how many trades were closed.
        P2: this now does real work — it flattens the in-memory position and writes the
        CLOSED row to the ledger so stats_lib sees it. Single choke point (rule: no
        scattered write paths) — apex_api just forwards the id here."""
        with self._lock:
            closed = 0
            if tradeid == "all":
                targets = list(self.positions)
            else:
                targets = [t for t in self.positions if str(t.get("trade_id")) == str(tradeid)]
            for t in targets:
                price = self.strategy.price(self.cycles) if self.strategy else float(t.get("open_rate", 0.0))
                if self._close_synthetic(t, price, exit_reason="force_exit"):
                    closed += 1
            return closed

    def _open_synthetic(self, pair, side="long", enter_tag=None):
        """Translate a strategy ENTER signal into one paper trade. Sized from
        config stake_amount, priced off the synthetic close, fee from config["fee"].
        Always pessimistic (APEX_PLAN 4.4): the fill is at the taker side and BOTH
        entry and exit fees are charged, so the paper P&L never flatters the live one."""
        fee = float(self.config.get("fee", 0.0005))
        price = self.strategy.price(self.cycles)
        stake = float(self.config.get("stake_amount", self.starting_capital))
        amount = (stake * (1.0 - fee)) / price            # taker-side fill reduces size
        trade_id = int(self.next_trade_id)
        self.next_trade_id += 1
        trade = {
            "trade_id": trade_id, "id": trade_id, "pair": pair,
            "base_currency": str(pair.split("/")[0]), "quote_currency": "USDC",
            "is_open": True, "is_short": (side == "short"),
            "exchange": "apex", "strategy": "ApexSynthetic", "enter_tag": enter_tag,
            "timeframe": 60, "trading_mode": "futures", "leverage": 1.0,
            "amount": amount, "stake_amount": stake,
            "open_rate": price, "open_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "open_timestamp": int(time.time() * 1000),
            "open_cycle": self.cycles,
            "current_rate": price, "profit_ratio": 0.0, "profit_pct": 0.0,
            "profit_abs": 0.0, "fee_open_cost": stake * fee,
            "fee_close_cost": None, "funding_fees": 0.0, "orders": [],
        }
        trade_id = apex_store.open_trade(trade)            # adopt the id sqlite assigned
        trade["id"] = trade_id
        trade["trade_id"] = trade_id
        self.positions.append(trade)
        return trade

    def _close_synthetic(self, trade, price, exit_reason="synthetic_hold_expired"):
        """Flatten one open paper trade, book the P&L, write the CLOSED ledger row.
        profit = price move minus BOTH entry and exit fees (pessimistic per APEX_PLAN 4.4).
        Returns True if a position was actually closed."""
        if not trade.get("is_open"):
            return False
        fee = float(self.config.get("fee", 0.0005))
        open_rate = float(trade.get("open_rate", price))
        direction = -1.0 if trade.get("is_short") else 1.0
        gross = direction * (price - open_rate) / open_rate
        profit_ratio = gross - 2.0 * fee                     # entry + exit fee
        profit_abs = trade.get("stake_amount", 0.0) * profit_ratio
        trade.update({
            "is_open": False, "close_rate": price, "current_rate": price,
            "close_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
            "exit_reason": exit_reason, "profit_ratio": profit_ratio,
            "profit_pct": profit_ratio * 100.0, "profit_abs": profit_abs,
            "fee_close_cost": trade.get("stake_amount", 0.0) * fee,
            "funding_fees": 0.0,
        })
        ok = apex_store.close_trade(
            trade["id"], price, close_profit=profit_ratio,
            close_profit_abs=profit_abs, exit_reason=exit_reason,
            fee_close_cost=trade["stake_amount"] * fee)
        if ok:
            with self._lock:
                self.positions = [t for t in self.positions if t.get("id") != trade["id"]]
                self.closed.append(trade)
            self.balance = self.starting_capital + sum(t.get("profit_abs", 0.0) for t in self.closed)
        return ok

    def cycle_strategy(self):
        """One strategy tick (only when config["synthetic"]). Builds the plain-dict state
        the strategy expects, acts on its signal."""
        if not self.strategy:
            return
        with self._lock:
            open_state = [{"trade_id": t["trade_id"], "open_cycle": t.get("open_cycle", self.cycles)}
                          for t in self.positions]
            sig = self.strategy.signal({
                "cycle": self.cycles, "paused": self.paused,
                "max_open": int(self.config.get("max_open_trades", 3)),
                "open": open_state,
            })
        action = (sig or {}).get("action")
        if action == "enter":
            self._open_synthetic(sig.get("pair", "BTC/USDC"),
                                 side=sig.get("side", "long"), enter_tag=sig.get("enter_tag"))
        elif action == "exit":
            with self._lock:
                target = next((t for t in self.positions if t.get("trade_id") == sig.get("trade_id")), None)
            if target:
                price = self.strategy.price(self.cycles)
                self._close_synthetic(target, price, exit_reason=sig.get("exit_reason", "exit_signal"))

    def note_cycle(self):
        with self._lock:
            self.cycles += 1
            self.last_cycle = time.time()


def heartbeat_file(state):
    """Liveness artifact for humans/P2 tooling. Atomic (state_io) so a SIGKILL during
    logout can't leave a half-written file that a loader silently swallows."""
    snap = state.snapshot()
    save_json(STATE_FILE, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "phase": "P2-paper" if state.strategy else "P1-flat",
        "paper_trades": len(state.closed),
        "open_trades": len(snap["positions"]),
        "paused": snap["paused"],
        "cycles": snap["cycles"],
    }, indent=2)


def main():
    ap = argparse.ArgumentParser(description="ApeX Omni bot daemon (P1: flat-forever)")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"apex_engine: cannot load {args.config}: {type(e).__name__}: {e}", flush=True)
        return 1

    # P1/P2 have no live order path; a live config here would mean unmanaged capital.
    if not cfg.get("dry_run", True):
        print("apex_engine: REFUSING to start — dry_run is false and the paper engine "
              "has no real order path. Flip dry_run back to true (APEX_PLAN P3 owns going live).", flush=True)
        return 1

    state = EngineState(cfg)
    throttle = float((cfg.get("internals") or {}).get("process_throttle_secs", DEFAULT_THROTTLE))
    port = int((cfg.get("api_server") or {}).get("listen_port", 8085))

    try:
        apex_api.serve_in_thread(state)
    except Exception as e:
        # No shim means watchdog reports the bot down, which is the honest outcome —
        # but the process still has nothing to do, so fail loudly and let KeepAlive retry.
        print(f"apex_engine: REST shim failed to start on :{port}: "
              f"{type(e).__name__}: {e}", flush=True)
        return 1

    mode = "PAPER (synthetic)" if state.strategy else "FLAT-FOREVER"
    print(f"apex_engine: up — dry_run wallet ${state.balance:.2f}, shim on :{port}, "
          f"throttle {throttle}s, {mode} (P2)", flush=True)

    last_beat = 0.0
    while True:
        try:
            state.note_cycle()
            # ---- P2: strategy tick -> fill sim -> ledger (synthetic, no exchange) ----
            state.cycle_strategy()
            now = time.time()
            if now - last_beat >= HEARTBEAT_EVERY:
                last_beat = now
                snap = state.snapshot()
                print(f"apex_engine: heartbeat cycles={snap['cycles']} "
                      f"balance={snap['balance']:.2f} open=0 "
                      f"paused={snap['paused']}", flush=True)
                heartbeat_file(state)
        except Exception as e:
            print(f"apex_engine: cycle error: {type(e).__name__}: {e}", flush=True)
        time.sleep(throttle)


if __name__ == "__main__":
    sys.exit(main() or 0)
