#!/usr/bin/env python
"""Freqtrade CLI wrapper that registers the CoinDCX adapter first.

WHY a wrapper: freqtrade builds its Exchange inside `_init_ccxt` before any
user module (strategy, hyperopt loss, pairlist) is imported, so there is no
in-tree hook that runs early enough to inject a non-ccxt exchange. Importing
`coindcx_ft` here — then delegating to freqtrade's real entrypoint — is the
only ordering that works.

Use exactly like the `freqtrade` binary:
    .venv/bin/python scripts/ft_coindcx.py backtesting --config config_sol_scalp_coindcx.json ...
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "user_data"))

import coindcx_ft  # noqa: E402,F401 — import side effect IS the point

from freqtrade.main import main  # noqa: E402

if __name__ == "__main__":
    main()
