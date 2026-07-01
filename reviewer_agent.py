"""Reviewer Agent - second AI that validates trade decisions before execution."""
import json
import logging
from dataclasses import dataclass, field

from openai import OpenAI

import config
import modelbound_skills
import openai_budget

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """You are a risk-focused gatekeeper for a prediction market trading agent. Your job is to REJECT bad trades. The agent has a history of overconfident, miscalibrated bets — especially low-price longshots and commodity bracket markets.

## Edge Math
- For YES trades: edge = AI Probability - YES price.
- For NO trades: edge = (100 - AI Probability) - NO price.

## REJECT if ANY of these apply:
- The agent's AI probability looks miscalibrated (e.g., claims 60%+ on a contract priced below 25¢)
- No research findings support the trade direction
- The math is wrong or the claimed edge contradicts the stated probability and price
- The research DIRECTLY contradicts the bet
- The event already happened or the bet is impossible under settlement rules
- The position would concentrate too much of a tiny account
- You are uncertain — uncertainty means REJECT, not approve
- Low-price YES longshots without authoritative settlement data cited in research

## APPROVE only if:
- Research clearly supports the directional bet with specific evidence
- The edge math is coherent and plausible (not fantasy 50¢ edges on 5¢ contracts)
- The market price and agent estimate divergence is explained with concrete facts
- You would bet your own money at this price

## REDIRECT (suggest alternative) if:
- The research contradicts THIS bet but supports a nearby alternative in the available markets list
- When redirecting: set approved=false, fill suggested_ticker, suggested_side, suggested_price

## Key principle: One bad longshot costs many small wins. Reject aggressively.

Respond with JSON only:
{
  "approved": true or false,
  "confidence": 0-100,
  "concerns": ["list specific issues"],
  "recommendation": "Brief note",
  "suggested_ticker": "ALTERNATIVE-TICKER or empty string if no suggestion",
  "suggested_side": "yes or no, or empty string",
  "suggested_price": 0
}"""


@dataclass
class ReviewResult:
    approved: bool = False
    confidence: int = 50
    concerns: list = field(default_factory=list)
    recommendation: str = ""
    suggested_ticker: str = ""
    suggested_side: str = ""
    suggested_price: int = 0


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
                approved=False,
                confidence=0,
                concerns=["Review skipped by OpenAI daily budget guard"],
                recommendation="Rejecting: cannot validate without review",
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
                approved=result_data.get("approved", False),
                confidence=min(100, max(0, result_data.get("confidence", 50))),
                concerns=result_data.get("concerns", []),
                recommendation=result_data.get("recommendation", ""),
                suggested_ticker=result_data.get("suggested_ticker", ""),
                suggested_side=result_data.get("suggested_side", ""),
                suggested_price=result_data.get("suggested_price", 0),
            )

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
            logger.warning(f"Reviewer JSON parse error: {e}. Rejecting by default.")
            return ReviewResult(
                approved=False,
                confidence=30,
                concerns=["Review response unparseable"],
                recommendation="Rejecting: review output invalid",
            )
        except Exception as e:
            logger.warning(f"Reviewer API error: {e}. Rejecting by default.")
            return ReviewResult(
                approved=False,
                confidence=0,
                concerns=[f"Review failed: {e}"],
                recommendation="Rejecting: review unavailable",
            )

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

        if related_markets:
            prompt += "\n## AVAILABLE ALTERNATIVE MARKETS (same event, different brackets)\n"
            for m in related_markets[:10]:
                prompt += f"- {m['ticker']}: \"{m.get('title', '')}\" | YES bid/ask: {m.get('yes_bid', '?')}¢/{m.get('yes_ask', '?')}¢\n"
            prompt += "\nIf rejecting, pick the best alternative from above based on the research data.\n"

        prompt += "\nEvaluate this trade. Approve only with strong evidence; otherwise reject. Respond with JSON."
        return prompt
