#!/usr/bin/env python3
"""
apex_client.py — the ApeX Omni SDK wrapper. PHASE P2: SURFACE ONLY, NOT WIRED.

This file exists so P3 has one choke point for every byte that crosses the wire to
ApeX — auth, market data, orders, positions, account. Nothing in P2 calls it: the
paper engine runs on a synthetic price series (apex_strategy) precisely so the
plumbing (signal -> fill -> sqlite -> ops layer) can be proven without an exchange,
an SDK, or a funded wallet anywhere near it.

THE ONE RULE THIS FILE ENFORCES: `apexomni` is imported INSIDE methods, never at
module top. `apexomni` pins setuptools<81 and pulls web3, and installing it into
./.venv would put the 5 CURRENTLY RUNNING freqtrade bots (pinned ccxt 4.5.65) at
risk (APEX_PLAN STATUS caveat). A module-level import would make `import
apex_client` — and therefore `import apex_engine` if it ever imports this — fail on
the live venv, taking the shim on :8085 down with it. Lazy imports keep this module
importable everywhere and push the failure to the exact call that needs the SDK.

Failure classes this file is written against:
  - silent degradation: a missing SDK raises RuntimeError with the install caveat in
    the message, never returns None/{} that a caller mistakes for "no positions".
    A shim reporting a flat book while real risk is on is APEX_PLAN 4.7.
  - a live order escaping in dry_run: place_order refuses outright when dry_run is
    set. In dry_run the paper fill simulator in apex_engine owns fills; if control
    ever reaches here, something is wired wrong and it must stop, not trade.
  - unbounded polling: the engine owns all exchange I/O on its own throttle
    (APEX_PLAN 4.5). Nothing in apex_api may ever construct this class.
"""

SDK_HINT = ("apexomni is not installed in ./.venv, and installing it there would put "
            "the 5 running freqtrade bots (pinned ccxt 4.5.65) at risk — it pins "
            "setuptools<81 and pulls web3. Spike it in a throwaway venv first "
            "(APEX_PLAN STATUS).")

TESTNET = "testnet"
MAINNET = "mainnet"


class ApexClient:
    """Thin wrapper over the apexomni HTTP clients. P2 ships the surface; P3 fills in
    retries, the rate-limit budget, and WS reconnect behind these same five methods so
    callers never learn the SDK's shape."""

    def __init__(self, config, network=TESTNET):
        self.config = config or {}
        self.network = network
        self.dry_run = bool(self.config.get("dry_run", True))
        self._priv = None          # signed/private client, set by connect_testnet()
        self._pub = None           # public client, set lazily by get_market_data()

    # ---- SDK BOUNDARY: every import below is function-local, on purpose ----

    def connect_testnet(self):
        """Build the signed private client against ApeX testnet and cache it.

        P0 proved this path in a throwaway venv; the credentials live in
        config_apex.secret.json (git-ignored) and are read from the merged config, so
        no key material is ever hard-coded here."""
        try:
            from apexomni.constants import APEX_OMNI_HTTP_TEST, NETWORKID_OMNI_TEST_BNB
            from apexomni.http_private_sign import HttpPrivateSign
        except ImportError as e:
            raise RuntimeError(f"apexomni not installed ({e}). {SDK_HINT}") from e

        creds = self.config.get("apex_credentials") or {}
        self._priv = HttpPrivateSign(
            APEX_OMNI_HTTP_TEST,
            network_id=NETWORKID_OMNI_TEST_BNB,
            api_key_credentials={
                "key": creds.get("key", ""),
                "secret": creds.get("secret", ""),
                "passphrase": creds.get("passphrase", ""),
            },
            zk_seeds=creds.get("zk_seeds", ""),
            zk_l2Key=creds.get("zk_l2key", ""),
        )
        self._priv.configs_v3()          # populates symbol/precision metadata
        return self._priv

    def get_market_data(self, symbol):
        """Depth + klines for one symbol off the PUBLIC endpoint (no auth needed).

        Returns the far side of the book too, because the paper fill simulator must
        fill at the taker side, never at mid (APEX_PLAN 4.4)."""
        try:
            from apexomni.constants import APEX_OMNI_HTTP_TEST
            from apexomni.http_public import HttpPublic
        except ImportError as e:
            raise RuntimeError(f"apexomni not installed ({e}). {SDK_HINT}") from e

        if self._pub is None:
            self._pub = HttpPublic(APEX_OMNI_HTTP_TEST)
        depth = self._pub.depth_v3(symbol=symbol, limit=5)
        klines = self._pub.klines_v3(symbol=symbol, interval=60, limit=200)
        return {"symbol": symbol, "depth": depth, "klines": klines}

    def place_order(self, symbol, side, size, price=None, order_type="MARKET",
                    reduce_only=False, client_id=None):
        """Submit an order. REFUSES in dry_run — the paper fill simulator inside
        apex_engine owns every dry-run fill, so reaching this method with dry_run set
        means a write path escaped the one choke point (APEX_PLAN 2)."""
        if self.dry_run:
            raise RuntimeError("apex_client.place_order called with dry_run=true — "
                               "paper fills belong to apex_engine's simulator, not the "
                               "exchange. Refusing to send.")
        try:
            from apexomni.constants import ORDER_SIDE_BUY, ORDER_SIDE_SELL
        except ImportError as e:
            raise RuntimeError(f"apexomni not installed ({e}). {SDK_HINT}") from e

        cli = self._require_connected()
        sd = ORDER_SIDE_BUY if str(side).upper() in ("BUY", "LONG") else ORDER_SIDE_SELL
        return cli.create_order_v3(symbol=symbol, side=sd, type=order_type,
                                   size=str(size), price=str(price) if price else None,
                                   reduceOnly=bool(reduce_only), clientId=client_id)

    def get_positions(self):
        """Live positions, for boot reconciliation (APEX_PLAN 4.7): the engine loads
        sqlite, fetches these, reconciles, and only then serves /status — otherwise a
        KeepAlive restart reports a flat book while real risk is on."""
        try:
            import apexomni  # noqa: F401  — presence check; the call goes via self._priv
        except ImportError as e:
            raise RuntimeError(f"apexomni not installed ({e}). {SDK_HINT}") from e
        return (self._require_connected().get_account_v3() or {}).get("positions", [])

    def get_account(self):
        """Account equity + collateral. Feeds /balance in live mode; in dry_run the
        shim serves the simulated wallet instead and never calls this."""
        try:
            import apexomni  # noqa: F401  — presence check; the call goes via self._priv
        except ImportError as e:
            raise RuntimeError(f"apexomni not installed ({e}). {SDK_HINT}") from e
        return self._require_connected().get_account_balance_v3()

    # ---- helpers ----

    def _require_connected(self):
        """Honest failure, not a None deref two frames later."""
        if self._priv is None:
            raise RuntimeError("apex_client: not connected — call connect_testnet() first.")
        return self._priv


def sdk_available():
    """True iff apexomni can be imported here. Diagnostics only — no caller may branch
    into a degraded trading path on this; a missing SDK is a hard stop, not a mode."""
    try:
        import apexomni  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    print(f"apex_client: apexomni importable = {sdk_available()}")
    print(f"apex_client: expected in P2 = False ({SDK_HINT})")
