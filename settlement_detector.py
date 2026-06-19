"""Settlement detection - polls Kalshi API and updates scorecard with outcomes."""
import json
import logging
import os
from datetime import datetime

from kalshi_client import KalshiClient
import config

logger = logging.getLogger(__name__)

SCORECARD_FILE = "data/scorecard.json"


class SettlementDetector:
    """Detects settled markets and updates the prediction scorecard."""

    def __init__(self, kalshi_client: KalshiClient):
        self.client = kalshi_client

    def check_settlements(self) -> list:
        """
        Query Kalshi for settlements, match against scorecard predictions.
        Returns list of newly resolved predictions.
        """
        try:
            resp = self.client.get_settlements()
            if "error" in resp:
                logger.warning(f"Settlements API error: {resp}")
                return []

            settlements = resp.get("settlements", [])
            if not settlements:
                return []

            logger.info(f"Found {len(settlements)} settlements from API")

        except Exception as e:
            logger.warning(f"Failed to check settlements: {e}")
            return []

        # Load current scorecard
        scorecard = self._load_scorecard()
        predictions = scorecard.get("predictions", [])

        # Find predictions that match settlements. Reconcile already-settled rows too
        # so bad dashboard writes or incomplete PnL fields can be corrected.
        resolved = []
        settlement_map = {s.get("ticker"): s for s in settlements}

        for idx, pred in enumerate(predictions):
            ticker = pred.get("ticker", "")
            if ticker in settlement_map:
                settlement = settlement_map[ticker]
                result = self._match_settlement_to_prediction(settlement, pred)
                if result and self._prediction_needs_update(pred, result):
                    result["_prediction_index"] = idx
                    resolved.append(result)

        # Update scorecard if we resolved or corrected anything
        if resolved:
            self._update_scorecard(scorecard, resolved)
            logger.info(f"Reconciled {len(resolved)} predictions: "
                       f"{sum(1 for r in resolved if r['result'] == 'win')} wins, "
                       f"{sum(1 for r in resolved if r['result'] == 'loss')} losses")

        for result in resolved:
            result.pop("_prediction_index", None)
        return resolved

    def _match_settlement_to_prediction(self, settlement: dict, prediction: dict) -> dict | None:
        """Determine if a settlement matches a prediction and compute result."""
        outcome = settlement.get("market_result", settlement.get("result", settlement.get("settled_outcome", "")))

        if not outcome:
            return None

        pred_side = prediction.get("side", "")

        pred_side = pred_side.lower()
        if pred_side == "yes":
            settled_count = float(settlement.get("yes_count_fp", settlement.get("yes_count", 0)) or 0)
            total_cost_dollars = float(settlement.get("yes_total_cost_dollars", 0) or 0)
        else:
            settled_count = float(settlement.get("no_count_fp", settlement.get("no_count", 0)) or 0)
            total_cost_dollars = float(settlement.get("no_total_cost_dollars", 0) or 0)

        if settled_count <= 0:
            logger.info(
                f"Skipping settlement for {prediction.get('ticker')}: "
                f"Kalshi reports 0 settled {pred_side.upper()} contracts"
            )
            return None

        # Determine win/loss
        # Settlement outcome is typically "yes" or "no"
        if outcome.lower() == pred_side:
            result = "win"
        else:
            result = "loss"

        prediction_count = int(prediction.get("count") or settled_count)

        # Calculate PnL
        pnl = self._calculate_pnl(
            prediction,
            result,
            settlement,
            settled_count,
            total_cost_dollars,
            prediction_count,
        )

        return {
            "ticker": prediction.get("ticker"),
            "title": prediction.get("title", ""),
            "side": pred_side,
            "price": prediction.get("price", 0),
            "count": prediction_count,
            "result": result,
            "outcome": outcome,
            "pnl_cents": pnl,
            "settled_at": datetime.utcnow().isoformat(),
        }

    def _calculate_pnl(
        self,
        prediction: dict,
        result: str,
        settlement: dict | None = None,
        settled_count: float | None = None,
        total_cost_dollars: float | None = None,
        prediction_count: int | None = None,
    ) -> int:
        """
        Calculate realized PnL in cents.
        Win: count * (100 - price) minus settlement fees
        Loss: -(count * price)
        """
        if settlement is not None and settled_count is not None and total_cost_dollars is not None:
            count = int(prediction_count or settled_count)
            gross_revenue_cents = int(round(count * 100)) if result == "win" else 0
            if settled_count > 0:
                cost_cents = int(round(total_cost_dollars * 100 * (count / settled_count)))
            else:
                cost_cents = int(round(total_cost_dollars * 100))
            try:
                fee_cents = float(settlement.get("fee_cost", 0) or 0) * 100
                if settled_count > 0:
                    fee_cents = fee_cents * (count / settled_count)
                fee_cents = int(round(fee_cents))
            except (TypeError, ValueError):
                fee_cents = 0
            return gross_revenue_cents - cost_cents - fee_cents

        price = prediction.get("price", 0)
        count = prediction.get("count", 1)  # Default to 1 if not stored

        if result == "win":
            gross_pnl = count * (100 - price)
            fees = int(gross_pnl * config.SETTLEMENT_FEE_PCT)
            return gross_pnl - fees
        else:
            return -(count * price)

    def _prediction_needs_update(self, prediction: dict, resolved: dict) -> bool:
        """True when Kalshi settlement data would change this scorecard row."""
        return any(
            prediction.get(key) != resolved.get(key)
            for key in ("result", "outcome", "pnl_cents", "count")
        )

    def _update_scorecard(self, scorecard: dict, resolved: list):
        """Write resolved predictions back to scorecard, recalculate totals."""
        predictions = scorecard.get("predictions", [])

        # Update predictions with results
        for r in resolved:
            idx = r.get("_prediction_index")
            if idx is None or idx >= len(predictions):
                continue
            pred = predictions[idx]
            pred["result"] = r["result"]
            pred["settled_at"] = r["settled_at"]
            pred["pnl_cents"] = r["pnl_cents"]
            pred["outcome"] = r["outcome"]
            pred["side"] = r["side"]
            pred["count"] = r["count"]

        # Recalculate totals
        wins = sum(1 for p in predictions if p.get("result") == "win")
        losses = sum(1 for p in predictions if p.get("result") == "loss")
        pending = sum(1 for p in predictions if not p.get("result"))
        total_pnl = sum(p.get("pnl_cents", 0) for p in predictions if p.get("pnl_cents"))

        scorecard["predictions"] = predictions
        scorecard["wins"] = wins
        scorecard["losses"] = losses
        scorecard["pending"] = pending
        scorecard["total_pnl_cents"] = total_pnl
        scorecard["win_rate"] = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        scorecard["last_updated"] = datetime.utcnow().isoformat()

        self._save_scorecard(scorecard)

    def _load_scorecard(self) -> dict:
        if os.path.exists(SCORECARD_FILE):
            with open(SCORECARD_FILE, "r") as f:
                return json.load(f)
        return {"predictions": [], "wins": 0, "losses": 0, "pending": 0}

    def _save_scorecard(self, scorecard: dict):
        os.makedirs(os.path.dirname(SCORECARD_FILE), exist_ok=True)
        with open(SCORECARD_FILE, "w") as f:
            json.dump(scorecard, f, indent=2)
