"""Reviewer Agent - second AI that validates trade decisions before execution."""
import json
import logging
from dataclasses import dataclass, field

from openai import OpenAI

import config
import modelbound_skills
import openai_budget

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """You are a quick sanity-check for a prediction market trading agent. Your bias should be toward APPROVING trades. The agent has already done research and calculated an edge — you're just catching obvious errors.

## Edge Math
- For YES trades: edge = AI Probability - YES price.
- For NO trades: edge = (100 - AI Probability) - NO price.
- A market implied probability that differs from the agent estimate is the whole reason to trade. Do NOT call that a contradiction or math error when the formula above gives a positive edge.

## Only REJECT if:
- The math is clearly wrong (e.g., agent says 70% probability but calculated a negative edge)
- The research DIRECTLY contradicts the bet (e.g., forecast says 90°F but agent bets on under 80°F)
- The agent is betting on something that already happened or is impossible
- The position size would blow up the account

## APPROVE if:
- The reasoning is coherent, even if you'd estimate differently
- The edge is positive and the logic makes sense
- It's a live event and the agent has a directional view
- The research supports the direction even if it's not conclusive
- You're uncertain — uncertainty means APPROVE with lower confidence

## REDIRECT (suggest alternative) if:
- The research contradicts THIS specific bet but SUPPORTS a nearby alternative
- Example: forecast says 93°F, agent bet on 95-96° bracket → suggest the 92-93° or 93-94° bracket instead
- Example: agent bet on Team A winning by 5+, but data suggests a close game → suggest Team A just winning
- When redirecting: set approved=false, and fill in suggested_ticker, suggested_side, suggested_price from the AVAILABLE MARKETS list provided

## Key principle: We'd rather make 10 trades and lose on 3 than make 0 trades. Volume with positive expected value beats perfection.

Respond with JSON only:
{
  "approved": true or false,
  "confidence": 0-100,
  "concerns": ["only list CRITICAL issues, not nitpicks"],
  "recommendation": "Brief note",
  "suggested_ticker": "ALTERNATIVE-TICKER or empty string if no suggestion",
  "suggested_side": "yes or no, or empty string",
  "suggested_price": 0
}"""


@dataclass
class ReviewResult:
    approved: bool = True
    confidence: int = 50
    concerns: list = field(default_factory=list)
    recommendation: str = ""
    suggested_ticker: str = ""  # Alternative ticker if rejecting (redirect trade)
    suggested_side: str = ""  # Side for the suggested alternative
    suggested_price: int = 0  # Price for the suggested alternative


class ReviewerAgent:
    """Second AI that validates trade decisions before execution."""

    def __init__(self):
        self.client = OpenAI(api_key=config.OPENAI_KEY)
        self.model = getattr(config, "REVIEWER_MODEL", "gpt-4o-mini")
        self.enabled = getattr(config, "REVIEWER_ENABLED", True)
        logger.info(f"ReviewerAgent initialized (enabled={self.enabled}, model={self.model})")

    def review_trade(self, trade: dict, context: dict) -> ReviewResult:
        """
        Review a proposed trade decision.

        Args:
            trade: The proposed trade (ticker, side, count, price, reasoning)
            context: Full context including market_data, research, rules, etc.

        Returns:
            ReviewResult with approval decision and reasoning.
        """
        if not self.enabled:
            return ReviewResult(approved=True, confidence=100,
                              concerns=[], recommendation="Review disabled")

        if not openai_budget.can_spend("review"):
            return ReviewResult(
                approved=True,
                confidence=0,
                concerns=["Review skipped by OpenAI daily budget guard"],
                recommendation="Proceeding after deterministic pre-flight checks",
            )

        review_prompt = self._build_review_prompt(trade, context)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                    {"role": "user", "content": review_prompt},
                ],
                temperature=0.2,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )

            result_text = response.choices[0].message.content.strip()
            openai_budget.record_spend("review")
            result_data = json.loads(result_text)

            review = ReviewResult(
                approved=result_data.get("approved", True),
                confidence=min(100, max(0, result_data.get("confidence", 50))),
                concerns=result_data.get("concerns", []),
                recommendation=result_data.get("recommendation", ""),
                suggested_ticker=result_data.get("suggested_ticker", ""),
                suggested_side=result_data.get("suggested_side", ""),
                suggested_price=result_data.get("suggested_price", 0),
            )

            override = self._deterministic_override(review, trade, context)
            if override:
                logger.info(
                    "Reviewer rejection overridden by deterministic edge checks "
                    f"for {trade.get('ticker', '?')}: {override.recommendation}"
                )
                review = override

            status = "APPROVED" if review.approved else "REJECTED"
            logger.info(
                f"Review {status} (confidence: {review.confidence}%) "
                f"for {trade.get('side', '?').upper()} {trade.get('ticker', '?')} "
                f"@ {trade.get('price', '?')}¢"
            )
            if review.concerns:
                logger.info(f"  Concerns: {'; '.join(review.concerns[:3])}")

            return review

        except json.JSONDecodeError as e:
            logger.warning(f"Reviewer JSON parse error: {e}. Approving by default.")
            return ReviewResult(approved=True, confidence=30,
                              concerns=["Review response unparseable"],
                              recommendation="Proceeding with caution")
        except Exception as e:
            logger.warning(f"Reviewer API error: {e}. Approving by default.")
            return ReviewResult(approved=True, confidence=0,
                              concerns=[f"Review failed: {e}"],
                              recommendation="Review skipped due to error")

    def _build_review_prompt(self, trade: dict, context: dict) -> str:
        """Build the review prompt with full trade context."""
        market_data = context.get("market_data", {})
        research = context.get("research", "No research available")
        reasoning = trade.get("reasoning", context.get("reasoning", "No reasoning provided"))
        rules = context.get("rules", "No settlement rules available")
        research_quality = context.get("research_quality", "unknown")
        related_markets = context.get("related_markets", [])
        side = str(trade.get("side", "?")).lower()
        price = trade.get("price", 0)
        ai_probability = trade.get("my_probability", 0)
        if side == "no":
            claimed_edge = (100 - ai_probability) - price
        else:
            claimed_edge = ai_probability - price

        prompt = f"""## Proposed Trade
- Market: {trade.get('ticker', '?')} - "{market_data.get('title', trade.get('title', '?'))}"
- Side: {side.upper()}
- Contracts: {trade.get('count', 0)}
- Price: {price}¢
- AI Probability: {ai_probability}%
- Claimed Edge: {claimed_edge}¢

## Settlement Rules
{rules}

## Research Findings (quality: {research_quality})
{research[:1000] if isinstance(research, str) else json.dumps(research, default=str)[:1000]}

## Trading Agent's Reasoning
{reasoning[:800]}

## Market Context
- YES bid/ask: {market_data.get('yes_bid', '?')}¢/{market_data.get('yes_ask', '?')}¢
- Spread: {market_data.get('spread', '?')}¢
- 24h Volume: ${market_data.get('volume', 0):.0f}
- Closes: {market_data.get('days_to_close', market_data.get('close_time', '?'))}

## Knowledge Base Context
{modelbound_skills.get_reviewer_context(market_data.get('category', 'general'))}
"""

        # Add related markets for redirect suggestions
        if related_markets:
            prompt += "\n## AVAILABLE ALTERNATIVE MARKETS (same event, different brackets)\n"
            for m in related_markets[:10]:
                prompt += f"- {m['ticker']}: \"{m.get('title', '')}\" | YES bid/ask: {m.get('yes_bid', '?')}¢/{m.get('yes_ask', '?')}¢\n"
            prompt += "\nIf rejecting, pick the best alternative from above based on the research data.\n"

        prompt += "\nEvaluate this trade. Approve, or redirect to a better alternative. Respond with JSON."
        return prompt

    def _deterministic_override(self, review: ReviewResult, trade: dict, context: dict) -> ReviewResult | None:
        """Approve false-negative reviewer rejects when deterministic checks are strong."""
        if review.approved:
            return None

        side = str(trade.get("side", "yes")).lower()
        price = self._safe_number(trade.get("price", 0))
        ai_probability = self._safe_number(trade.get("my_probability", 0))
        if 0 < ai_probability <= 1:
            ai_probability *= 100

        if side == "no":
            calculated_edge = (100 - ai_probability) - price
        else:
            calculated_edge = ai_probability - price

        is_speculative = bool(trade.get("speculative", False))
        min_edge = config.SPECULATIVE_EDGE_MIN_CENTS if is_speculative else config.MIN_EDGE_CENTS
        market_data = context.get("market_data", {})
        spread = self._safe_number(market_data.get("spread", 99))
        volume = self._safe_number(market_data.get("volume", 0))
        days_to_close = self._safe_number(market_data.get("days_to_close", 999))
        concerns_text = " ".join(review.concerns + [review.recommendation]).lower()

        if calculated_edge < min_edge or price < config.MIN_TRADE_PRICE_CENTS:
            return None
        if self._has_hard_rejection_reason(concerns_text):
            return None

        math_false_reject = any(
            token in concerns_text
            for token in ("math", "calculation", "miscalculation", "implied probability", "claimed edge", "does not support")
        )
        no_research_reject = "no research" in concerns_text or "without supporting data" in concerns_text
        liquid_market = volume >= 500 and spread <= 5
        strong_liquid_market = volume >= 1000 and spread <= 2 and calculated_edge >= max(10, min_edge + 3)
        near_term = days_to_close <= 2

        if math_false_reject and liquid_market:
            return ReviewResult(
                approved=True,
                confidence=max(35, min(review.confidence, 60)),
                concerns=["Reviewer math rejection overridden by deterministic edge formula"],
                recommendation=f"Proceeding: deterministic edge is {calculated_edge:.1f}¢.",
            )

        if no_research_reject and strong_liquid_market and near_term:
            return ReviewResult(
                approved=True,
                confidence=35,
                concerns=["Limited research, but liquid near-term market and deterministic edge pass"],
                recommendation=f"Proceeding cautiously with {calculated_edge:.1f}¢ edge.",
            )

        return None

    def _has_hard_rejection_reason(self, concerns_text: str) -> bool:
        hard_reasons = (
            "already happened",
            "impossible",
            "position size",
            "blow up",
            "directly contradicts",
            "research directly contradicts",
            "head-to-head",
            "historical performance suggests",
            "forecast says",
            "settlement rules contradict",
            "invalid",
        )
        return any(reason in concerns_text for reason in hard_reasons)

    def _safe_number(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
