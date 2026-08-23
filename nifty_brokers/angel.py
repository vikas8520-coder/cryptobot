#!/usr/bin/env python3
"""Angel One (SmartAPI) broker adapter."""
import logging
from datetime import datetime

from .base import Broker

logger = logging.getLogger(__name__)


class AngelBroker(Broker):
    """
    Live broker via Angel One SmartAPI.
    Requires:
      - api_key
      - client_code
      - password
      - totp (or refresh token)
    See https://smartapi.angelbroking.com/docs/
    """

    def __init__(self, api_key=None, client_code=None, password=None,
                 totp=None, refresh_token=None, **kwargs):
        self.api_key = api_key
        self.client_code = client_code
        self.password = password
        self.totp = totp
        self.refresh_token = refresh_token
        self.smart = None

    def authenticate(self):
        if not self.api_key or not self.client_code:
            logger.error("Angel: api_key and client_code are required")
            return False
        try:
            from SmartApi import SmartConnect
            self.smart = SmartConnect(api_key=self.api_key)
            session = self.smart.generateSession(
                self.client_code, self.password, self.totp or self.refresh_token
            )
            if not session.get("status"):
                logger.error("Angel session failed: %s", session)
                return False
            self.refresh_token = session["data"]["refreshToken"]
            self.smart.setToken(session["data"]["jwtToken"])
            return True
        except Exception as e:
            logger.error("Angel auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.smart:
            self.authenticate()
        try:
            return self.smart.position()["data"] or []
        except Exception as e:
            logger.error("Angel get_positions failed: %s", e)
            return []

    def _symboltoken(self, signal):
        # Angel uses numeric symbol tokens. In practice you need to look them up
        # in the master scrip JSON. This is a placeholder.
        return "0"

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.smart:
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
            params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": self._symboltoken(signal),
                "transactiontype": "BUY",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": "INTRADAY",
                "duration": "DAY",
                "quantity": str(qty),
            }
            return self.smart.placeOrder(params)
        except Exception as e:
            logger.error("Angel place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.smart:
            self.authenticate()
        try:
            params = {
                "variety": "NORMAL",
                "tradingsymbol": position["tradingsymbol"],
                "symboltoken": position["symboltoken"],
                "transactiontype": "SELL",
                "exchange": position.get("exchange", "NFO"),
                "ordertype": "MARKET",
                "producttype": position.get("producttype", "INTRADAY"),
                "duration": "DAY",
                "quantity": str(position.get("quantity", 0)),
            }
            return self.smart.placeOrder(params)
        except Exception as e:
            logger.error("Angel close_position failed: %s", e)
            return None
