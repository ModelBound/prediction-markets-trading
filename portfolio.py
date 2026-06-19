"""Portfolio management and risk control."""
import json
import os
import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)

DATA_DIR = "data"
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio_state.json")
TRADES_FILE = os.path.join(DATA_DIR, "trade_history.json")
CYCLE_LOG_FILE = os.path.join(DATA_DIR, "cycle_logs.json")


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_json(filepath, default=None):
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(filepath, data):
    ensure_data_dir()
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)


class PortfolioManager:
    """Manages portfolio state, risk checks, and performance tracking."""

    def __init__(self, kalshi_client):
        self.client = kalshi_client
        self.state = load_json(PORTFOLIO_FILE, {
            "cash_balance": 0,
            "positions": [],
            "total_account_value": 0,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "cycle_spend": 0,
            "last_cycle_reset": None,
        })
        self.trade_history = load_json(TRADES_FILE, [])

    def save_state(self):
        save_json(PORTFOLIO_FILE, self.state)

    def save_trades(self):
        save_json(TRADES_FILE, self.trade_history)

    async def refresh_from_api(self):
        """Refresh portfolio state from Kalshi API."""
        balance_resp = self.client.get_balance()
        if "error" not in balance_resp:
            self.state["cash_balance"] = balance_resp.get("balance", 0)
            logger.info(f"Balance: ${self.state['cash_balance'] / 100:.2f}")

        positions_resp = self.client.get_positions()
        if "error" not in positions_resp:
            raw_positions = positions_resp.get("market_positions", [])
            self.state["positions"] = self._process_positions(raw_positions)

        self._calculate_account_value()
        self.save_state()

    def _process_positions(self, raw_positions: list) -> list:
        """Process raw position data into our format."""
        positions = []
        for pos in raw_positions:
            # Handle both old format (position) and new format (position_fp)
            quantity_raw = pos.get("position_fp", pos.get("position", 0))
            quantity = abs(int(float(quantity_raw))) if quantity_raw else 0

            total_traded = pos.get("total_traded_dollars", pos.get("total_traded", 0))
            if quantity == 0 and not total_traded:
                continue

            if quantity == 0:
                continue

            # Determine side from position sign (positive = yes, negative = no)
            pos_val = float(pos.get("position_fp", pos.get("position", 0)) or 0)
            side = "yes" if pos_val > 0 else "no"

            # Calculate entry price from total cost / quantity
            total_cost_str = pos.get("market_exposure_dollars", pos.get("total_traded_dollars", pos.get("total_cost_dollars", "0")))
            total_cost_cents = int(float(total_cost_str or "0") * 100)
            entry_price = total_cost_cents // quantity if quantity > 0 else 0

            positions.append({
                "ticker": pos.get("ticker", ""),
                "market_ticker": pos.get("market_ticker", pos.get("ticker", "")),
                "side": side,
                "quantity": quantity,
                "entry_price": entry_price,
                "current_bid": 0,  # Will be updated with market data
                "cost_basis": total_cost_cents,
            })

        return positions

    def _calculate_account_value(self):
        """Calculate total account value (cash + positions at bid)."""
        position_value = sum(
            pos["quantity"] * pos.get("current_bid", pos["entry_price"])
            for pos in self.state["positions"]
        )
        self.state["total_account_value"] = self.state["cash_balance"] + position_value
        self.state["unrealized_pnl"] = sum(
            pos["quantity"] * (pos.get("current_bid", pos["entry_price"]) - pos["entry_price"])
            for pos in self.state["positions"]
        )

    def update_position_prices(self, market_prices: dict):
        """Update current bid prices for all positions."""
        for pos in self.state["positions"]:
            ticker = pos["ticker"]
            if ticker in market_prices:
                if pos["side"] == "yes":
                    pos["current_bid"] = market_prices[ticker].get("yes_bid", pos["entry_price"])
                else:
                    pos["current_bid"] = market_prices[ticker].get("no_bid", pos["entry_price"])

        self._calculate_account_value()
        self.save_state()

    # --- Risk Checks ---

    def check_concentration(self, ticker: str, additional_cost: int) -> bool:
        """Check if trade would exceed concentration limit.

        For small bankrolls (<$10), we allow up to 80% in one market
        since diversification isn't practical with $2-5.
        """
        existing_cost = sum(
            pos["cost_basis"] for pos in self.state["positions"]
            if pos["ticker"] == ticker
        )
        total_cost = existing_cost + additional_cost
        account_value = max(self.state["total_account_value"], 1)

        # For small accounts, relax concentration to allow meaningful trades
        if account_value < 1000:  # Under $10
            max_concentration = 0.80  # Allow up to 80%
        else:
            max_concentration = config.MAX_CONCENTRATION_PCT

        concentration = total_cost / account_value
        if concentration > max_concentration:
            logger.warning(
                f"Concentration limit: {ticker} would be {concentration:.1%} "
                f"(max {max_concentration:.0%})"
            )
            return False
        return True

    def check_solvency(self, trade_cost: int) -> bool:
        """Check if we have enough cash for the trade including fees."""
        estimated_fees = int(trade_cost * config.TRADING_FEE_PCT)
        total_needed = trade_cost + estimated_fees
        if self.state["cash_balance"] < total_needed:
            logger.warning(
                f"Solvency check failed: need {total_needed}¢, have {self.state['cash_balance']}¢"
            )
            return False
        return True

    def check_cycle_limit(self, trade_cost: int) -> bool:
        """Check if trade would exceed per-cycle spending limit.

        For small accounts (<$10), allow spending up to 100% of bankroll per cycle
        since the whole point is to deploy the bankroll.
        """
        account_value = max(self.state["total_account_value"], 1)

        if account_value < 1000:  # Under $10 - let the bankroll be fully deployed
            max_spend = account_value
        else:
            max_spend = int(account_value * config.MAX_CYCLE_SPEND_PCT)
        if self.state["cycle_spend"] + trade_cost > max_spend:
            logger.warning(
                f"Cycle limit: spent {self.state['cycle_spend']}¢ + {trade_cost}¢ "
                f"would exceed max {max_spend}¢"
            )
            return False
        return True

    def validate_trade(self, ticker: str, count: int, price: int) -> tuple[bool, str]:
        """Run all risk checks for a proposed trade. Returns (valid, reason)."""
        trade_cost = count * price

        if not self.check_solvency(trade_cost):
            return False, "Insufficient funds (including fees)"

        if not self.check_concentration(ticker, trade_cost):
            return False, f"Would exceed {config.MAX_CONCENTRATION_PCT:.0%} concentration limit"

        if not self.check_cycle_limit(trade_cost):
            return False, "Would exceed per-cycle spending limit"

        if price < 1 or price > 99:
            return False, f"Invalid price: {price}¢ (must be 1-99)"

        if count < 1:
            return False, f"Invalid count: {count}"

        return True, "OK"

    def reset_cycle_spend(self):
        """Reset per-cycle spending counter."""
        self.state["cycle_spend"] = 0
        self.state["last_cycle_reset"] = datetime.utcnow().isoformat()
        self.save_state()

    def record_trade(self, trade_data: dict):
        """Record a completed trade."""
        trade_cost = trade_data.get("count", 0) * trade_data.get("price", 0)
        self.state["cycle_spend"] += trade_cost

        self.trade_history.append({
            **trade_data,
            "timestamp": datetime.utcnow().isoformat(),
        })
        self.save_state()
        self.save_trades()

    # --- Position Sizing ---

    def kelly_size(self, probability: float, market_price: int) -> int:
        """Calculate position size using quarter-Kelly criterion."""
        if market_price <= 0 or market_price >= 100:
            return 0

        p = probability
        q = 1 - p
        b = (100 - market_price) / market_price  # odds against

        if b <= 0:
            return 0

        kelly = (p * b - q) / b
        quarter_kelly = kelly * config.KELLY_FRACTION

        if quarter_kelly <= 0:
            return 0

        account_value = max(self.state["total_account_value"], 1)
        max_spend = int(account_value * quarter_kelly)
        contracts = max_spend // market_price

        # Apply concentration limit
        max_contracts = int(account_value * config.MAX_CONCENTRATION_PCT) // market_price
        contracts = min(contracts, max_contracts)

        # Apply cycle limit
        remaining_cycle_budget = int(account_value * config.MAX_CYCLE_SPEND_PCT) - self.state["cycle_spend"]
        max_from_cycle = remaining_cycle_budget // market_price
        contracts = min(contracts, max_from_cycle)

        return max(0, contracts)

    # --- Performance Metrics ---

    def get_performance_summary(self) -> dict:
        """Get performance metrics."""
        if not self.trade_history:
            return {"total_trades": 0, "win_rate": 0, "total_pnl": 0}

        settled = [t for t in self.trade_history if t.get("realized_pnl") is not None]
        wins = [t for t in settled if t.get("realized_pnl", 0) > 0]

        return {
            "total_trades": len(self.trade_history),
            "settled_trades": len(settled),
            "win_rate": len(wins) / len(settled) if settled else 0,
            "total_realized_pnl": sum(t.get("realized_pnl", 0) for t in settled),
            "unrealized_pnl": self.state["unrealized_pnl"],
            "account_value": self.state["total_account_value"],
            "cash_balance": self.state["cash_balance"],
            "open_positions": len(self.state["positions"]),
        }
