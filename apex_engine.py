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
from state_io import save_json

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE, "config_apex.json")
STATE_FILE = os.path.join(BASE, "apex_state.json")   # git-ignored by the *_state.json rule

DEFAULT_THROTTLE = 5      # seconds; mirrors config.json:77 internals.process_throttle_secs
HEARTBEAT_EVERY = 300     # seconds between log lines — bot_apex.log is rotated, not infinite


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
        P1 is flat by construction, so this is always 0 — but it stays the ONE choke
        point every exit path must go through, which is where P2's order/fill logic
        lands (no scattered write paths, per APEX_PLAN 2)."""
        with self._lock:
            return 0 if not self.positions else len(self.positions)

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
        "phase": "P1-flat",
        "balance": snap["balance"],
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

    # P1 has no order path at all; a live config here would mean unmanaged capital.
    if not cfg.get("dry_run", True):
        print("apex_engine: REFUSING to start — dry_run is false and P1 has no order "
              "path. Flip dry_run back to true (APEX_PLAN P3 owns going live).", flush=True)
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

    print(f"apex_engine: up — dry_run wallet ${state.balance:.2f}, shim on :{port}, "
          f"throttle {throttle}s, FLAT-FOREVER (P1, no exchange I/O)", flush=True)

    last_beat = 0.0
    while True:
        try:
            state.note_cycle()
            # ---- P2 lands here: candle top-up -> strategy -> fill sim -> store ----
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
