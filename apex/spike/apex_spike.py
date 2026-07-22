#!/usr/bin/env python3
"""
apex_spike.py — P0 THROWAWAY spike (NOT part of the cryptobot system).

Goal: prove the apexomni SDK can talk to ApeX and return real market data,
using the isolated /tmp/apex_spike_venv (never the live .venv). Public market-data
endpoints need NO auth, so this validates connectivity + response shapes end to end.
Authenticated calls (account/equity) are stubbed until testnet API creds exist.

Run: /tmp/apex_spike_venv/bin/python apex_spike.py
"""
import json
from apexomni.http_public import HttpPublic
from apexomni.constants import APEX_OMNI_HTTP_TEST, APEX_OMNI_HTTP_MAIN

SYMBOL = "BTCUSDT"


def show(label, obj, depth=0):
    """Print a compact preview of an API response so we SEE real data, not 'ok'."""
    s = json.dumps(obj, default=str)
    print(f"\n=== {label} ===")
    print(s[:900] + (" ..." if len(s) > 900 else ""))


def main():
    # try testnet first; fall back to mainnet public data if testnet is down
    for name, endpoint in [("TESTNET", APEX_OMNI_HTTP_TEST),
                           ("MAINNET", APEX_OMNI_HTTP_MAIN)]:
        print(f"\n########## {name}: {endpoint} ##########")
        try:
            client = HttpPublic(endpoint)
        except Exception as e:
            print(f"  client init failed: {type(e).__name__}: {e}")
            continue

        # 1) server time — cheapest liveness probe
        try:
            show("server_time", client.server_time())
        except Exception as e:
            print(f"  server_time failed: {type(e).__name__}: {e}")

        # 2) klines — the OHLCV feed the strategy will need
        try:
            k = client.klines_v3(symbol=SYMBOL, interval="60", limit=3)
            show("klines_v3 (BTCUSDT 1h x3)", k)
        except Exception as e:
            print(f"  klines_v3 failed: {type(e).__name__}: {e}")

        # 3) depth — the order book the paper fill-simulator will price against
        try:
            d = client.depth_v3(symbol=SYMBOL, limit=5)
            show("depth_v3 (BTCUSDT top5)", d)
        except Exception as e:
            print(f"  depth_v3 failed: {type(e).__name__}: {e}")

        # 4) ticker — last/mark price
        try:
            show("ticker_v3", client.ticker_v3(symbol=SYMBOL))
        except Exception as e:
            print(f"  ticker_v3 failed: {type(e).__name__}: {e}")

        print(f"\n  [{name}] AUTH/account check: SKIPPED — needs testnet "
              "key+secret+passphrase from on-chain registration (not yet provided).")


if __name__ == "__main__":
    main()
