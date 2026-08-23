#!/usr/bin/env python3
"""Zerodha Kite Connect broker adapter."""
import logging

from .base import Broker

logger = logging.getLogger(__name__)


class ZerodhaBroker(Broker):
    """
    Live broker via Zerodha Kite Connect.
    Requires:
      - api_key
      - api_secret  (for token generation)
      - access_token (or refresh token flow)
    See https://kite.trade/docs/connect/v3/
    """

    def __init__(self, api_key=None, access_token=None, **kwargs):
        self.api_key = api_key
        self.access_token = access_token
        self.kite = None

    def authenticate(self):
        if not self.api_key or not self.access_token:
            logger.error("Zerodha: api_key and access_token are required")
            return False
        try:
            from kiteconnect import KiteConnect
            self.kite = KiteConnect(api_key=self.api_key, access_token=self.access_token)
            # Lightweight profile call to verify token
            self.kite.profile()
            return True
        except Exception as e:
            logger.error("Zerodha auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.kite:
            if not self.authenticate():
                return []
        try:
            return self.kite.positions().get("net", [])
        except Exception as e:
            logger.error("Zerodha get_positions failed: %s", e)
            return []

    def _tradingsymbol(self, signal):
        """Build a Zerodha option symbol from the signal.
        Example: NIFTY25AUG24250CE.
        """
        from datetime import datetime
        expiry = datetime.strptime(signal["expiry"], "%Y-%m-%d")
        month_str = expiry.strftime("%y%b").upper()
        strike = int(signal["strike"])
        opt = "CE" if signal["side"] == "call" else "PE"
        return f"NIFTY{month_str}{strike}{opt}"

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.kite:
            if not self.authenticate():
                return None
        from nifty_paper_order import LOT
        from .base import PaperBroker
        lots = PaperBroker().place_order(signal, capital=capital, risk_pct=risk_pct)
        qty = int(round(lots) * LOT)
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=self._tradingsymbol(signal),
                transaction_type=self.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=self.kite.ORDER_TYPE_MARKET,
                product=self.kite.PRODUCT_MIS,
            )
            logger.info("Zerodha order placed: %s", order_id)
            return order_id
        except Exception as e:
            logger.error("Zerodha place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.kite:
            if not self.authenticate():
                return None
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=position.get("exchange", self.kite.EXCHANGE_NFO),
                tradingsymbol=position["tradingsymbol"],
                transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                quantity=position["quantity"],
                order_type=self.kite.ORDER_TYPE_MARKET,
                product=position.get("product", self.kite.PRODUCT_MIS),
            )
            return order_id
        except Exception as e:
            logger.error("Zerodha close_position failed: %s", e)
            return None
