#!/usr/bin/env python3
"""Fyers broker adapter."""
import logging
from datetime import datetime

from .base import Broker

logger = logging.getLogger(__name__)


class FyersBroker(Broker):
    """
    Live broker via Fyers API v3.
    Requires:
      - client_id
      - access_token
      - pin
    See https://myapi.fyers.in/docs/
    """

    def __init__(self, client_id=None, access_token=None, pin=None, **kwargs):
        self.client_id = client_id
        self.access_token = access_token
        self.pin = pin
        self.fyers = None

    def authenticate(self):
        if not self.client_id or not self.access_token:
            logger.error("Fyers: client_id and access_token are required")
            return False
        try:
            from fyers_apiv3 import fyersModel
            self.fyers = fyersModel.FyersModel(
                client_id=self.client_id,
                token=self.access_token,
            )
            # Lightweight profile call
            self.fyers.get_profile(data={})
            return True
        except Exception as e:
            logger.error("Fyers auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.fyers:
            self.authenticate()
        try:
            return self.fyers.positions()["netPositions"] or []
        except Exception as e:
            logger.error("Fyers get_positions failed: %s", e)
            return []

    def _symbol(self, signal):
        expiry = datetime.strptime(signal["expiry"], "%Y-%m-%d")
        month = expiry.strftime("%y%b").upper()
        opt = "CE" if signal["side"] == "call" else "PE"
        # Fyers symbol format: NSE:NIFTY25AUG24250CE
        return f"NSE:NIFTY{month}{int(signal['strike'])}{opt}"

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.fyers:
            self.authenticate()
        from nifty_paper_order import LOT
        from .base import PaperBroker
        lots = PaperBroker().place_order(signal, capital=capital, risk_pct=risk_pct)
        qty = int(round(lots) * LOT)
        data = {
            "symbol": self._symbol(signal),
            "qty": qty,
            "type": 2,  # market
            "side": 1,  # buy
            "productType": "INTRADAY",
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
            "orderTag": "niftybot",
        }
        try:
            return self.fyers.place_order(data)
        except Exception as e:
            logger.error("Fyers place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.fyers:
            self.authenticate()
        data = {
            "symbol": position["symbol"],
            "qty": position.get("qty", 0),
            "type": 2,
            "side": -1,
            "productType": position.get("productType", "INTRADAY"),
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        try:
            return self.fyers.place_order(data)
        except Exception as e:
            logger.error("Fyers close_position failed: %s", e)
            return None
