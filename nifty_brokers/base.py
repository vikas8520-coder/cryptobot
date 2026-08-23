#!/usr/bin/env python3
"""Base broker interface and paper broker implementation."""
from abc import ABC, abstractmethod

import nifty_paper_order as porder


class Broker(ABC):
    """Abstract broker interface for NSE option trades."""

    @abstractmethod
    def authenticate(self):
        """Set up session / access token. Return True on success."""
        pass

    @abstractmethod
    def get_positions(self):
        """Return list of currently open positions (broker-specific dicts)."""
        pass

    @abstractmethod
    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        """Open a new option position. Returns order id/None or lots placed."""
        pass

    @abstractmethod
    def close_position(self, position, reason="manual"):
        """Close an existing position. Returns order id/None."""
        pass


class PaperBroker(Broker):
    """Paper broker that journals virtual trades to SQLite."""

    def __init__(self, db=porder.DB, capital=100000.0, risk_pct=0.02, **kwargs):
        # kwargs absorbs unused config keys for factory compatibility
        self.conn = porder.init_db(db)
        self.capital = capital
        self.risk_pct = risk_pct

    def authenticate(self):
        return True

    def get_positions(self):
        return porder.get_open_trades(self.conn)

    def place_order(self, signal, capital=None, risk_pct=None):
        if capital is None:
            capital = self.capital
        if risk_pct is None:
            risk_pct = self.risk_pct
        return porder.open_trade(self.conn, signal, capital=capital, risk_pct=risk_pct)

    def close_position(self, position, reason="manual"):
        # Paper positions are closed by the alert/order daemon on target/stop/eod.
        return None

    def process_open_positions(self, df, send_fn=None):
        """Update/close any open paper positions using the latest 5m bars."""
        return porder.process_open_trades(self.conn, df, send_fn=send_fn)
