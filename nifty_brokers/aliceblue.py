#!/usr/bin/env python3
"""Alice Blue broker adapter."""
import logging
from datetime import datetime

from .base import Broker

logger = logging.getLogger(__name__)


class AliceBlueBroker(Broker):
    """
    Live broker via Alice Blue.
    Requires:
      - client_id
      - api_key
      - password (for login)
    See https://v2api.aliceblueonline.com/
    """

    def __init__(self, client_id=None, api_key=None, password=None,
                 redirect_url=None, **kwargs):
        self.client_id = client_id
        self.api_key = api_key
        self.password = password
        self.redirect_url = redirect_url
        self.alice = None

    def authenticate(self):
        if not self.client_id or not self.api_key:
            logger.error("AliceBlue: client_id and api_key are required")
            return False
        try:
            from alice_blue import AliceBlue
            self.alice = AliceBlue(
                client_id=self.client_id,
                api_key=self.api_key,
                redirect_url=self.redirect_url or "",
            )
            # login_and_get_sessionID may need TOTP/answer based on 2FA
            self.alice.get_session_id(self.password)
            return True
        except Exception as e:
            logger.error("AliceBlue auth failed: %s", e)
            return False

    def get_positions(self):
        if not self.alice:
            self.authenticate()
        try:
            return self.alice.get_daywise_positions()["data"]
        except Exception as e:
            logger.error("AliceBlue get_positions failed: %s", e)
            return []

    def _instrument(self, signal):
        expiry = datetime.strptime(signal["expiry"], "%Y-%m-%d")
        month = expiry.strftime("%y%b").upper()
        opt = "CE" if signal["side"] == "call" else "PE"
        return f"NIFTY{month}{int(signal['strike'])}{opt}"

    def place_order(self, signal, capital=100000.0, risk_pct=0.02):
        if not self.alice:
            self.authenticate()
        from nifty_paper_order import LOT
        from .base import PaperBroker
        lots = PaperBroker().place_order(signal, capital=capital, risk_pct=risk_pct)
        qty = int(round(lots) * LOT)
        try:
            return self.alice.place_order(
                transaction_type=self.alice.TransactionType.Buy,
                instrument=self.alice.get_instrument_by_symbol("NFO", self._instrument(signal)),
                quantity=qty,
                order_type=self.alice.OrderType.Market,
                product_type=self.alice.ProductType.Intraday,
                price=0.0,
                trigger_price=None,
                stop_loss=None,
                square_off=None,
                trailing_sl=None,
                is_amo=False,
            )
        except Exception as e:
            logger.error("AliceBlue place_order failed: %s", e)
            return None

    def close_position(self, position, reason="manual"):
        if not self.alice:
            self.authenticate()
        try:
            return self.alice.place_order(
                transaction_type=self.alice.TransactionType.Sell,
                instrument=position["instrument"],
                quantity=position.get("quantity", 0),
                order_type=self.alice.OrderType.Market,
                product_type=position.get("product_type", self.alice.ProductType.Intraday),
                price=0.0,
            )
        except Exception as e:
            logger.error("AliceBlue close_position failed: %s", e)
            return None
