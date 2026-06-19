"""Polymarket provider scaffold.

This adapter is intentionally conservative: it documents the method surface
needed by the trading loop, but it does not place real orders until a
contributor wires in Polymarket's CLOB client and settlement model.
"""
import os


class PolymarketClient:
    """Placeholder client for future Polymarket support."""

    def __init__(self):
        self.api_key = os.getenv("POLYMARKET_API_KEY")
        self.private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        self.funder_address = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        raise NotImplementedError(
            "Polymarket support is a scaffold. Implement polymarket_client.py "
            "with Polymarket CLOB auth, market normalization, order placement, "
            "positions, and settlement reconciliation before enabling it."
        )

    def get_markets(self, status: str = "open", limit: int = 100, cursor: str | None = None) -> dict:
        raise NotImplementedError

    def get_market(self, ticker: str) -> dict:
        raise NotImplementedError

    def get_balance(self) -> dict:
        raise NotImplementedError

    def get_positions(self, status: str | None = None) -> dict:
        raise NotImplementedError

    def get_settlements(self) -> dict:
        raise NotImplementedError

    def get_fills(self, limit: int = 50) -> dict:
        raise NotImplementedError

    def get_orders(self, status: str | None = None) -> dict:
        raise NotImplementedError

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: int,
        action: str = "buy",
        order_type: str = "limit",
    ) -> dict:
        raise NotImplementedError
