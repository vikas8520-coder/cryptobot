#!/usr/bin/env python3
"""
nifty_brokers — pluggable broker adapters for NSE option trading.

Supports paper trading plus the most common Indian discount brokers.
Use `get_broker(name, config_path)` to instantiate the right adapter.
"""
import json
import os

from .base import Broker, PaperBroker
from .zerodha import ZerodhaBroker
from .upstox import UpstoxBroker
from .angel import AngelBroker
from .fivepaisa import FivePaisaBroker
from .fyers import FyersBroker
from .aliceblue import AliceBlueBroker

__all__ = [
    "Broker",
    "PaperBroker",
    "ZerodhaBroker",
    "UpstoxBroker",
    "AngelBroker",
    "FivePaisaBroker",
    "FyersBroker",
    "AliceBlueBroker",
    "get_broker",
    "list_brokers",
]

BROKERS = {
    "paper": PaperBroker,
    "zerodha": ZerodhaBroker,
    "upstox": UpstoxBroker,
    "angel": AngelBroker,
    "fivepaisa": FivePaisaBroker,
    "fyers": FyersBroker,
    "aliceblue": AliceBlueBroker,
}


def list_brokers():
    return sorted(BROKERS.keys())


def _load_config(config_path):
    if not config_path:
        return {}
    with open(config_path) as f:
        return json.load(f)


def get_broker(name, config_path=None, config=None):
    """
    Return a broker instance by name.
    `config` takes priority; otherwise `config_path` is loaded.
    Broker-specific keys are read from the nested section, with top-level
    capital/risk_pct available as defaults.
    """
    name = (name or "paper").lower()
    cls = BROKERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown broker '{name}'. Available: {list_brokers()}")
    cfg = {}
    if config_path:
        cfg = _load_config(config_path)
    if config:
        cfg = config
    broker_cfg = {}
    # top-level capital/risk_pct can override defaults
    for k in ("capital", "risk_pct"):
        if k in cfg:
            broker_cfg[k] = cfg[k]
    # broker-specific nested section
    if name in cfg and isinstance(cfg[name], dict):
        broker_cfg.update(cfg[name])
    return cls(**broker_cfg)
