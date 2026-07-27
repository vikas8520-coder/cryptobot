#!/usr/bin/env python3
"""
apex_auth_spike.py — P0 READ-ONLY authenticated check (THROWAWAY).

Proves the testnet API credentials authenticate and can read account/balance.
STRICTLY read-only: get_user, get_account, get_account_balance. NO order/transfer/
withdraw calls anywhere in this file. Isolated /tmp venv, creds from /tmp only.
"""
import json
from apexomni.http_private_v3 import HttpPrivate_v3
from apexomni.constants import APEX_OMNI_HTTP_TEST, APEX_OMNI_HTTP_MAIN, REGISTER_ENVID_TEST, REGISTER_ENVID_MAIN

creds = json.load(open("/tmp/apex_creds.json"))
KC = {"key": creds["key"], "secret": creds["secret"], "passphrase": creds["passphrase"]}


def show(label, obj):
    s = json.dumps(obj, default=str)
    print(f"\n=== {label} ===")
    print(s[:1200] + (" ..." if len(s) > 1200 else ""))


def try_env(name, endpoint, env_id):
    print(f"\n########## {name}: {endpoint} (env_id={env_id}) ##########")
    try:
        c = HttpPrivate_v3(endpoint, network_id=env_id, env_id=env_id, api_key_credentials=KC)
    except Exception as e:
        print(f"  client init failed: {type(e).__name__}: {e}")
        return False
    ok = False
    for meth in ["get_user_v3", "get_account_v3", "get_account_balance"]:
        try:
            res = getattr(c, meth)()
            show(meth, res)
            ok = True
        except Exception as e:
            print(f"  {meth} failed: {type(e).__name__}: {e}")
    return ok


if __name__ == "__main__":
    # testnet first (safe). Only fall through to mainnet if you explicitly flip this.
    if not try_env("TESTNET", APEX_OMNI_HTTP_TEST, REGISTER_ENVID_TEST):
        print("\n>>> Testnet auth returned nothing usable. NOT trying mainnet automatically.")
        print(">>> If these are mainnet creds, re-run with the mainnet block enabled deliberately.")
