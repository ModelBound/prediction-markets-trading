"""Prediction market provider factory and extension protocol.

Kalshi is the default production-ready provider. Contributors can add new
providers by implementing the small method surface used by TradingCycle,
PortfolioManager, SettlementDetector, and the dashboard.
"""
from typing import Protocol

import config
from kalshi_client import KalshiClient


class PredictionMarketClient(Protocol):
    """Protocol for prediction market integrations."""

    def get_markets(self, status: str = "open", limit: int = 100, cursor: str | None = None) -> dict:
        ...

    def get_market(self, ticker: str) -> dict:
        ...

    def get_balance(self) -> dict:
        ...

    def get_positions(self, status: str | None = None) -> dict:
        ...

    def get_settlements(self) -> dict:
        ...

    def get_fills(self, limit: int = 50) -> dict:
        ...

    def get_orders(self, status: str | None = None) -> dict:
        ...

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: int,
        action: str = "buy",
        order_type: str = "limit",
    ) -> dict:
        ...


def get_market_client() -> PredictionMarketClient:
    """Instantiate the configured prediction market provider."""
    provider = config.PREDICTION_MARKET_PROVIDER.lower()
    if provider == "kalshi":
        return KalshiClient()
    if provider == "polymarket":
        from polymarket_client import PolymarketClient

        return PolymarketClient()
    raise ValueError(f"Unsupported PREDICTION_MARKET_PROVIDER: {provider}")
