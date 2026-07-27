# apex/ — ApeX Omni bot (Path A, in progress)

Custom decentralized-trading bot using ApeX's native `apexomni` SDK, built to plug
into the existing cryptobot ops layer. See `../APEX_PLAN.md` for the full build spec.

## Status: P0 COMPLETE (spike only — no bot yet)

P0 proved the SDK works end to end:
- Isolated venv (`/tmp/apex_spike_venv`, Python 3.9, `apexomni==3.3.1` + `mnemonic`) —
  deliberately NOT the live `.venv`, to avoid web3/setuptools conflicts with the 5
  running freqtrade bots on ccxt 4.5.65.
- Public market data verified (testnet + mainnet): `server_time`, `klines_v3`,
  `depth_v3`, `ticker_v3`.
- Authenticated read verified on TESTNET (`env_id=5`): `get_user_v3`, `get_account_v3`
  (returns l2Key, zkAccountId, and live fee rates: taker 0.05% / maker 0.02%).
- Note: `get_account_balance` (v1) returns HTTP 409 — deprecated; use `get_account_v3`.

### Endpoints (from constants)
- Testnet HTTP: `https://qa.omni.apex.exchange`   WS: `wss://qa-quote.omni.apex.exchange`
- Mainnet HTTP: `https://omni.apex.exchange`       WS: `wss://quote.omni.apex.exchange`
- Response envelope: `{"data": {...}, "timeCost": <ms>}` — unwrap `data`.
- Klines fields are short: s/i/t/o/h/l/c/v/tr → map to standard OHLCV.

## Spike scripts (reference, `spike/`)
- `apex_spike.py` — public market-data probe (no auth).
- `apex_auth_spike.py` — READ-ONLY auth check (get_user/account only; no orders).

Both read credentials from `/tmp/apex_creds.json` (git-ignored location, chmod 600).
**No credentials are stored in this repo.**

## Not done yet
- Testnet account is authenticated but `isRegistered:false` and unfunded — needs
  registration + testnet USDC before P1 can exercise real fills.
- P1+ (REST shim on :8085, engine skeleton, paper fill simulator) not started —
  those touch live ops files (`watchdog.py`, `dashboard.py`, `local_secrets.py`) and
  must be done deliberately, not unattended.
