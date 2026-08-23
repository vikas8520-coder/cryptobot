#!/usr/bin/env python3
"""Upstox broker adapter."""
import logging
from datetime import datetime

from .base import Broker

logger = logging.getLogger(__name__)


class UpstoxBroker(Broker):
    """
    Live broker via Upstox API v2.
    Requires:
      - api_key
      - access_token
    See https://developer.upstox.com/docs/
    """

    def __init__(self, api_key=None, access_token=None, **kwargs):
        self.api_key = api_key
        self.access_token = access_token
        self.api = None

    def authenticate(self):
        if not self.api_key or not self.access_token:
            logger.error("Upstox: api_key and access_token are required")
            return False
        try:
            from upstox_python_api import Upstox
            self.api = Upstox(self.api_key, self.access_token)
            self.api.get_balance()
            return True
        except Exception as e:
            logger.error("Upstox auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.api:
            self.authenticate()
        try:
            return self.api.get_positions()
        except Exception as e:
            logger.error("Upstox get_positions failed: %s", e)
            return []

    def _instrument(self, signal):
        expiry = datetime.strptime(signal["expiry"], "%Y-%m-%d")
        month = expiry.strftime("%y%b").upper()
        opt = "CE" if signal["side"] == "call" else "PE"
        return f"NIFTY{month}{int(signal['strike'])}{opt}"

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.api:
            self.authenticate()
        from nifty_paper_order import LOT
        from .base import PaperBroker
        lots = PaperBroker().place_order(signal, capital=capital, risk_pct=risk_pct)
        qty = int(round(lots) * LOT)
        try:
            return self.api.place_order(
                transaction_type="B",
                exchange="NFO",
                symbol=self._instrument(signal),
                quantity=qty,
                order_type="M",
                product_type="I",  # MIS
                price=0,
                trigger_price=0,
            )
        except Exception as e:
            logger.error("Upstox place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.api:
            self.authenticate()
        try:
            return self.api.place_order(
                transaction_type="S",
                exchange=position.get("exchange", "NFO"),
                symbol=position["tradingsymbol"],
                quantity=position.get("quantity", 0),
                order_type="M",
                product_type=position.get("product_type", "I"),
                price=0,
                trigger_price=0,
            )
        except Exception as e:
            logger.error("Upstox close_position failed: %s", e)
            return None
