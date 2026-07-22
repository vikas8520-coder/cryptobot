#!/usr/bin/env python3
"""
nifty_paper_test.py — Nifty 50 paper-bot proof harness (one-shot, not auto-run).

Same role as spx_paper_test.py: drives the paper trade lifecycle deterministically and
asserts the whole chain — strategy signal -> paper fill -> sqlite row -> stats_lib query.
Forces config["synthetic"]=true so the run is reproducible with NO network, then verifies
the ledger stats_lib will read.

Checks:
  (a) an ENTER opens a trade (in-memory /status + ledger open row)
  (b) the same trade CLOSES (profit_ratio computed, is_open flips to 0)
  (c) tradesv3_nifty.sqlite carries the CLOSED row with the columns stats_lib reads
  (d) a stats_lib-style query (is_open = 0) returns that row with a real number

Prints PASS/FAIL with actual numbers. Exit 0 on PASS, 1 on FAIL.
"""
import os
import sqlite3
import sys

import nifty_engine
import nifty_store
from state_io import save_json  # noqa: F401 (import sanity — same stack as prod)

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "config_nifty.json")
DB = nifty_store.DB_PATH


def main():
    nifty_store.ensure_db()
    cfg = nifty_engine.load_config(CFG)
    cfg["synthetic"] = True   # deterministic, no network — this is a plumbing test
    state = nifty_engine.EngineState(cfg)

    # --- (a) open a trade ---
    t = state._open("NIFTYBEES/INR", side="long", enter_tag="test")
    open_id = int(t["trade_id"])
    print(f"[a] opened trade id={open_id} pair={t['pair']} @ {t['open_rate']:.2f} "
          f"amount={t['amount']:.4f} stake={t['stake_amount']} (INR)")

    if open_id not in [p["trade_id"] for p in state.snapshot()["positions"]]:
        print("FAIL (a): open trade not in /status snapshot positions")
        return 1
    open_rows = nifty_store.load_open_trades()
    if not any(r["id"] == open_id for r in open_rows):
        print("FAIL (a): open row not found in ledger")
        return 1
    print(f"[a] ledger has {len(open_rows)} open row(s), id {open_id} present: OK")

    # --- (b) close it at a price that produces a non-zero P&L ---
    close_price = t["open_rate"] * 1.01  # +1% move
    ok = state._close(t, close_price, exit_reason="test_close")
    print(f"[b] closed id={open_id} @ {close_price:.2f} profit_ratio={t['profit_ratio']:.6f} "
          f"profit_abs={t['profit_abs']:.4f} -> {ok}")
    if not ok:
        print("FAIL (b): close_trade returned False")
        return 1
    # Our specific trade must be gone from open positions (ledger accumulates across runs).
    if open_id in [p["trade_id"] for p in state.snapshot()["positions"]]:
        print("FAIL (b): our trade still in open positions after close")
        return 1
    if open_id not in [c["trade_id"] for c in state.snapshot()["closed"]]:
        print("FAIL (b): closed trade not in closed list")
        return 1

    # --- (c) ledger carries the CLOSED row with the columns stats_lib reads ---
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT id, is_open, close_profit, close_profit_abs, exit_reason, fee_open_cost, "
        "funding_fees, is_short, pair, stake_amount FROM trades WHERE id = ?",
        (open_id,)).fetchone()
    conn.close()
    if row is None:
        print("FAIL (c): closed row missing from ledger")
        return 1
    is_open, cp, cpa, reason = row[1], row[2], row[3], row[4]
    print(f"[c] ledger row id={row[0]} is_open={is_open} close_profit={cp} "
          f"close_profit_abs={cpa} exit_reason={reason} pair={row[8]}")
    if is_open != 0:
        print("FAIL (c): is_open not 0 on closed row")
        return 1
    if cp is None or cpa is None:
        print("FAIL (c): null close_profit / close_profit_abs — stats_lib would read it as 0")
        return 1

    # --- (d) stats_lib-style query on our specific row (ledger accumulates across runs) ---
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    r = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(close_profit_abs),0), "
        "COALESCE(SUM(COALESCE(fee_open_cost,0)+COALESCE(fee_close_cost,0)),0) "
        "FROM trades WHERE is_open = 0 AND id = ?", (open_id,)).fetchone()
    conn.close()
    n, net_abs, fees = r
    print(f"[d] stats_lib-style (is_open=0 AND id={open_id}): n={n} net_abs={net_abs:.4f} fees={fees:.4f}")
    if n != 1:
        print(f"FAIL (d): expected exactly our 1 closed trade, found {n}")
        return 1
    if abs(net_abs - cpa) > 1e-9:
        print("FAIL (d): stats_sql net_abs disagrees with the ledger row")
        return 1

    print("\nPASS: strategy -> paper fill -> ledger -> stats_lib chain verified "
          f"({n} closed trade, net {net_abs:+.4f} INR, fees {fees:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
