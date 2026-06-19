"""Kalshi API client with RSA-PSS authentication."""
import base64
import datetime
import json
import uuid
import logging
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

import config

logger = logging.getLogger(__name__)


class KalshiClient:
    """Client for interacting with the Kalshi Trade API."""

    def __init__(self):
        self.api_key_id = config.KALSHI_API_KEY_ID
        self.base_url = config.get_kalshi_base_url()
        self.private_key = self._load_private_key()
        logger.info(f"KalshiClient initialized. Mode: {config.TRADING_MODE}, URL: {self.base_url}")

    def _load_private_key(self):
        """Load RSA private key from environment variable."""
        key_string = config.KALSHI_API_RSA
        if not key_string:
            raise ValueError("KALSHI_API_RSA not set in environment")
        return serialization.load_pem_private_key(
            key_string.encode("utf-8"),
            password=None,
            backend=default_backend(),
        )

    def _create_signature(self, timestamp: str, method: str, path: str) -> str:
        """Create RSA-PSS SHA256 signature for request authentication."""
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _get_headers(self, method: str, path: str) -> dict:
        """Generate authenticated headers for a request."""
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        signature = self._create_signature(timestamp, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, data: dict = None, params: dict = None) -> dict:
        """Make an authenticated request to the Kalshi API."""
        url = f"{self.base_url}{endpoint}"
        sign_path = urlparse(url).path

        if params:
            param_str = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if param_str:
                url = f"{url}?{param_str}"

        headers = self._get_headers(method, sign_path)

        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=30)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=30)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if response.status_code >= 400:
                logger.error(f"API error {response.status_code}: {response.text}")
                return {"error": response.status_code, "message": response.text}

            return response.json() if response.text else {}

        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            return {"error": "request_failed", "message": str(e)}

    # --- Public Market Data ---

    def get_markets(self, status: str = "open", limit: int = 100, cursor: str = None) -> dict:
        """Get available markets."""
        params = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        """Get a specific market by ticker."""
        return self._request("GET", f"/markets/{ticker}")

    def get_market_orderbook(self, ticker: str) -> dict:
        """Get the orderbook for a specific market."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

    def get_event(self, event_ticker: str) -> dict:
        """Get event details."""
        return self._request("GET", f"/events/{event_ticker}")

    # --- Portfolio ---

    def get_balance(self) -> dict:
        """Get account balance (in cents)."""
        return self._request("GET", "/portfolio/balance")

    def get_positions(self, status: str = None) -> dict:
        """Get current positions."""
        params = {}
        if status:
            params["settlement_status"] = status
        return self._request("GET", "/portfolio/positions", params=params)

    def get_settlements(self) -> dict:
        """Get settlement history."""
        return self._request("GET", "/portfolio/settlements")

    def get_fills(self, limit: int = 50) -> dict:
        """Get fill history."""
        return self._request("GET", "/portfolio/fills", params={"limit": limit})

    # --- Orders ---

    def get_orders(self, status: str = None) -> dict:
        """Get orders."""
        params = {}
        if status:
            params["status"] = status
        return self._request("GET", "/portfolio/orders", params=params)

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: int,
        action: str = "buy",
        order_type: str = "limit",
    ) -> dict:
        """
        Place an order on Kalshi using the V2 endpoint.

        Args:
            ticker: Market ticker
            side: 'yes' or 'no'
            count: Number of contracts
            price: Price in cents (1-99)
            action: 'buy' (only action supported for now)
            order_type: 'limit'
        """
        # V2 uses 'bid' (buy YES) / 'ask' (sell YES).
        # Buying NO at X¢ is equivalent to selling YES at (100-X)¢.
        if side == "yes":
            v2_side = "bid"
            v2_price = f"{price / 100:.2f}"
        else:
            # Buying NO at `price` cents = selling YES at (100 - price) cents
            v2_side = "ask"
            v2_price = f"{(100 - price) / 100:.2f}"

        data = {
            "ticker": ticker,
            "side": v2_side,
            "count": f"{count:.2f}",
            "price": v2_price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": str(uuid.uuid4()),
        }

        logger.info(f"Placing order: {side.upper()} {count}x {ticker} @ {price}¢ (V2: {v2_side} @ ${v2_price})")
        return self._request("POST", "/portfolio/events/orders", data=data)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an order."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")

    # --- Convenience Methods ---

    def buy_yes(self, ticker: str, count: int, price: int) -> dict:
        """Buy YES contracts."""
        return self.place_order(ticker, "yes", count, price)

    def buy_no(self, ticker: str, count: int, price: int) -> dict:
        """Buy NO contracts (also used to sell YES via netting)."""
        return self.place_order(ticker, "no", count, price)

    def sell_yes(self, ticker: str, count: int, sell_price: int) -> dict:
        """Sell YES position by buying NO (reciprocal netting)."""
        no_price = 100 - sell_price
        return self.buy_no(ticker, count, no_price)

    def sell_no(self, ticker: str, count: int, sell_price: int) -> dict:
        """Sell NO position by buying YES (reciprocal netting)."""
        yes_price = 100 - sell_price
        return self.buy_yes(ticker, count, yes_price)
