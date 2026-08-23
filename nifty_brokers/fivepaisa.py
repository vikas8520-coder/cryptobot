#!/usr/bin/env python3
"""5paisa broker adapter."""
import logging
from datetime import datetime

from .base import Broker

logger = logging.getLogger(__name__)


class FivePaisaBroker(Broker):
    """
    Live broker via 5paisa py5paisa.
    Requires:
      - client_code, email, password, dob
    See https://pypi.org/project/py5paisa/
    """

    def __init__(self, client_code=None, email=None, password=None,
                 dob=None, app_name=None, **kwargs):
        self.client_code = client_code
        self.email = email
        self.password = password
        self.dob = dob
        self.app_name = app_name
        self.client = None

    def authenticate(self):
        if not self.client_code or not self.password:
            logger.error("5paisa: client_code and password are required")
            return False
        try:
            from py5paisa import FivePaisaClient
            cred = {
                "CLIENT_CODE": self.client_code,
                "PASSWORD": self.password,
                "EMAIL": self.email or "",
                "DOB": self.dob or "",
            }
            self.client = FivePaisaClient(cred=cred)
            self.client.login()
            return True
        except Exception as e:
            logger.error("5paisa auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.client:
            self.authenticate()
        try:
            return self.client.positions()
        except Exception as e:
            logger.error("5paisa get_positions failed: %s", e)
            return []

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.client:
            self.authenticate()
        from nifty_paper_order import LOT
        from .base import PaperBroker
        lots = PaperBroker().place_order(signal, capital=capital, risk_pct=risk_pct)
        qty = int(round(lots) * LOT)
        expiry = datetime.strptime(signal["expiry"], "%Y-%m-%d")
        month = expiry.strftime("%y%b").upper()
        opt = "CE" if signal["side"] == "call" else "PE"
        symbol = f"NIFTY{month}{int(signal['strike'])}{opt}"
        try:
            return self.client.place_order(
                Exchange="N",
                ExchangeType="D",
                ScripCode=0,  # you must resolve ScripCode from 5paisa masters
                Price=0,
                Qty=qty,
                OrderType="BUY",
            )
        except Exception as e:
            logger.error("5paisa place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.client:
            self.authenticate()
        try:
            return self.client.place_order(
                Exchange=position.get("Exchange", "N"),
                ExchangeType=position.get("ExchangeType", "D"),
                ScripCode=position.get("ScripCode", 0),
                Price=0,
                Qty=position.get("Qty", 0),
                OrderType="SELL",
            )
        except Exception as e:
            logger.error("5paisa close_position failed: %s", e)
            return None
