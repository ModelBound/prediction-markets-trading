"""OpenAI-powered trading agent for market analysis and decision making."""
import json
import logging
from datetime import datetime

from openai import OpenAI

import config
import modelbound_skills
import openai_budget
import web_research

logger = logging.getLogger(__name__)

RESPONSE_FORMAT = """## Response Format
You MUST respond with valid JSON in this exact format:
{
  "reasoning": "Your detailed analysis of the current market state and opportunities",
  "opportunities": [
    {
      "ticker": "MARKET-TICKER",
      "title": "Market question",
      "side": "yes or no",
      "market_price": 65,
      "my_probability": 80,
      "edge": 15
    }
  ],
  "action": "trade" or "pass",
  "trades": [
    {
      "ticker": "MARKET-TICKER",
      "side": "yes" or "no",
      "count": 5,
      "price": 65,
      "my_probability": 80,
      "reasoning": "Why this specific trade"
    }
  ],
  "notes_update": "Any patterns or insights to remember for future cycles",
  "pass_reason": "If passing, explain why (only if action is pass)"
}
The "action" field is REQUIRED on every response. Use "trade" when placing orders, "pass" when skipping.
"""

SYSTEM_PROMPT = """You are an autonomous prediction market trading agent operating on Kalshi with real money. You execute a disciplined trading cycle, analyzing markets, researching opportunities, and making trade decisions.

## Core Philosophy
- Fundamental Trading: Identify where markets are mispriced relative to true probability
- Value Identification: Find edge where market price diverges from your estimated probability
- Favorite-Longshot Bias: Near-settlement favorites (85-95¢) are often systematically underpriced
- Conservative Sizing: Use quarter-Kelly criterion, never risk more than 15% in one market

## Decision Framework
- Only trade when estimated probability differs from market price by ≥10¢
- Always account for fees (~5% trading, ~1.4% settlement)
- Expected PnL = (Your Probability × $1.00) - Contract Cost - Fees
- Prefer liquid markets with tight bid-ask spreads (≤5¢)

## Risk Rules
- Max 15% of account in any single market
- Max 25% of account spent per cycle
- Diversify across categories (financial, weather, politics, sports, entertainment)
- PASSING is always a valid decision - don't force trades
- You CAN and SHOULD spread your budget across multiple markets if you find multiple edges
- Better to make 3-4 smaller bets across different events than one large bet

## What NOT to Do
- Don't trade without calculating expected value
- Don't ignore bid-ask spreads (they are real costs)
- Don't trade illiquid markets
- Don't chase losses
- Don't over-trade

""" + RESPONSE_FORMAT


def _compose_system_prompt(modelbound_prompt: str | None) -> str:
    """Ensure every system prompt includes the required JSON response schema."""
    base = modelbound_prompt.strip() if modelbound_prompt else SYSTEM_PROMPT
    if '"action"' in base and "Response Format" in base:
        return base
    return f"{base.rstrip()}\n\n{RESPONSE_FORMAT}"


def _opportunities_to_trades(opportunities: list) -> list:
    """Convert LLM opportunity objects into executable trade proposals."""
    trades = []
    for opp in opportunities:
        ticker = opp.get("ticker")
        if not ticker:
            continue
        price = opp.get("price") or opp.get("market_price") or 0
        trades.append({
            "ticker": ticker,
            "title": opp.get("title", ""),
            "side": str(opp.get("side", "yes")).lower(),
            "count": 1,
            "price": int(price),
            "my_probability": opp.get("my_probability", 0),
            "reasoning": opp.get("reasoning", ""),
        })
    return trades


def _normalize_decision(decision: dict) -> dict:
    """Repair common LLM response omissions before execution."""
    if not isinstance(decision, dict):
        return {
            "action": "pass",
            "pass_reason": "Invalid response type",
            "trades": [],
            "reasoning": "",
        }

    if "action" not in decision and "decision" in decision:
        decision["action"] = decision["decision"]

    trades = decision.get("trades") or []
    if not trades and decision.get("opportunities"):
        trades = _opportunities_to_trades(decision["opportunities"])
        if trades:
            decision["trades"] = trades

    if "action" not in decision:
        if trades:
            decision["action"] = "trade"
            logger.warning("LLM omitted action field; inferred trade from trades/opportunities")
        else:
            decision["action"] = "pass"
            decision["pass_reason"] = decision.get("pass_reason") or "No trade action specified"

    action = str(decision.get("action", "pass")).lower().strip()
    if action in {"buy", "execute", "trade"}:
        decision["action"] = "trade"
    else:
        decision["action"] = "pass"

    if decision["action"] == "trade" and not decision.get("trades"):
        decision["action"] = "pass"
        decision["pass_reason"] = "Trade action but no trades specified"

    return decision


class TradingAgent:
    """Trading agent powered by OpenAI."""

    def __init__(self):
        self.client = OpenAI(api_key=config.OPENAI_KEY)
        self.model = "gpt-4o-mini"  # Fast, cheap, good at structured JSON
        self.research_model = "gpt-4o-mini"  # For web-grounded research

        # Load system prompt from ModelBound (falls back to hardcoded)
        mb_prompt = modelbound_skills.get_system_prompt()
        self.system_prompt = _compose_system_prompt(mb_prompt)

        logger.info(f"TradingAgent initialized with model: {self.model}")
        if config.RESEARCH_PROVIDER == "free":
            import web_research
            backends = web_research.search_backend_status()
            logger.info(f"Research provider: free (Exa={backends['exa_mcporter']}, DDG={backends['duckduckgo']})")
        else:
            logger.info("Research provider: openai (web_search_preview)")
        if mb_prompt:
            logger.info("System prompt loaded from ModelBound")
        else:
            logger.info("Using hardcoded system prompt (ModelBound unavailable)")

    def analyze_and_decide(self, cycle_prompt: str) -> dict:
        """
        Send the cycle context to OpenAI and get a trading decision.

        Args:
            cycle_prompt: The full context for this trading cycle

        Returns:
            Parsed decision dict with reasoning, action, trades, etc.
        """
        if not openai_budget.can_spend("decision"):
            return {
                "action": "pass",
                "reasoning": "OpenAI daily budget guard skipped decision call",
                "pass_reason": "OpenAI daily budget reached",
                "trades": [],
            }

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": cycle_prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
                response_format={"type": "json_object"},
            )

            response_text = response.choices[0].message.content.strip()
            openai_budget.record_spend("decision")
            logger.info(f"OpenAI response length: {len(response_text)} chars")

            # Parse JSON response
            decision = _normalize_decision(json.loads(response_text))

            return decision

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenAI response as JSON: {e}")
            logger.error(f"Raw response: {response_text[:500]}")
            return {
                "action": "pass",
                "reasoning": "Failed to parse LLM response",
                "pass_reason": f"JSON parse error: {e}",
                "trades": [],
            }
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return {
                "action": "pass",
                "reasoning": f"API error: {e}",
                "pass_reason": f"LLM error: {e}",
                "trades": [],
            }

    def research_market(self, query: str) -> str:
        """
        Research a market question using free web search (default) or OpenAI web search.

        Args:
            query: Research question about a market

        Returns:
            Research findings as text
        """
        if config.RESEARCH_PROVIDER == "openai":
            return self._research_market_openai(query)
        return self._research_market_free(query)

    def _research_market_free(self, query: str) -> str:
        """Agent-Reach style research: DuckDuckGo/Exa search + Jina Reader (no API cost)."""
        try:
            result = web_research.research_query(query)
            logger.info(f"Free web research: {len(result)} chars for query: {query[:60]}")
            return result
        except Exception as e:
            logger.error(f"Free research query failed: {e}")
            return f"Research unavailable: {e}"

    def _research_market_openai(self, query: str) -> str:
        """Legacy OpenAI web_search_preview research (~$0.03/call)."""
        if not openai_budget.can_spend("research"):
            return "Research unavailable: OpenAI daily budget reached"

        try:
            response = self.client.responses.create(
                model=self.research_model,
                tools=[{"type": "web_search_preview"}],
                input=f"Research this prediction market question and provide factual, current information that would help estimate the probability of the outcome. Be concise and data-driven. Include specific numbers, dates, odds, or statistics where possible.\n\nQuestion: {query}",
            )

            # Extract text from response output
            text_parts = []
            for item in response.output:
                if hasattr(item, "content"):
                    for block in item.content:
                        if hasattr(block, "text"):
                            text_parts.append(block.text)
            openai_budget.record_spend("research")
            return " ".join(text_parts).strip() if text_parts else "No research results"

        except Exception as e:
            logger.error(f"Research query failed: {e}")
            return f"Research unavailable: {e}"

    def research_markets_batch(self, markets: list) -> dict:
        """
        Research the top market opportunities with thorough multi-query approach.

        Args:
            markets: List of market dicts with ticker, title, yes_bid, etc.

        Returns:
            Dict mapping ticker -> research findings
        """
        # Pick the most interesting NON-WEATHER markets to research.
        # (Weather already has free data from NWS/Open-Meteo)
        weather_series = {"KXHIGHTDC", "KXHIGHTNYC", "KXHIGHTCHI", "KXHIGHPHIL", "KXLOWTSEA", "KXTEMPNYCH", "KXHIGHCHI"}
        candidates = [
            m for m in markets
            if m.get("volume", 0) >= config.MIN_RESEARCH_VOLUME
            and 10 <= m.get("yes_bid", 0) <= 90
            and m["ticker"].split("-")[0] not in weather_series
        ]
        def _research_score(market: dict) -> float:
            """Favor near-term/live markets over long-dated high-volume markets."""
            hours_to_close = market.get("hours_to_close", 9999)
            volume = market.get("volume", 0)
            score = min(volume, 5000) / 10
            if market.get("is_live_event"):
                score += 1000
            if hours_to_close <= 6:
                score += 900
            elif hours_to_close <= 24:
                score += 600
            elif hours_to_close <= 72:
                score += 250
            elif hours_to_close > 720:
                score -= 500
            return score

        # Sort by freshness/opportunity score, but deduplicate by series (max 1 per series)
        seen_series = set()
        diverse_candidates = []
        for m in sorted(candidates, key=_research_score, reverse=True):
            series = m["ticker"].split("-")[0]
            if series not in seen_series:
                seen_series.add(series)
                diverse_candidates.append(m)
        candidates = diverse_candidates[:config.MAX_RESEARCH_MARKETS]

        if not candidates:
            candidates = markets[:1]

        research = {}
        for market in candidates:
            ticker = market["ticker"]
            title = market.get("title", "")
            rules = market.get("rules", "")
            yes_bid = market.get("yes_bid", 0)

            logger.info(f"Researching: {ticker} - {title[:50]}")
            result = self.research_market_thorough(market)

            # Skip if rate limited
            if "Research unavailable" in result.get("summary", "") and "429" in result.get("summary", ""):
                logger.warning("Rate limited - skipping remaining research")
                break

            research[ticker] = result.get("summary", "No findings")
            logger.info(f"Research for {ticker}: quality={result.get('research_quality', '?')}, sources={result.get('sources_count', 0)}")

        return research

    def research_market_thorough(self, market: dict) -> dict:
        """
        Conduct thorough research with multiple queries.

        Query 1: Primary event data (current state, latest news)
        Query 2: Specific data source from settlement rules
        Query 3 (if needed): Contradicting evidence

        Returns structured research result.
        """
        title = market.get("title", "")
        rules = market.get("rules", "")
        yes_bid = market.get("yes_bid", 0)

        findings = []
        contradictions = []

        # Query 1: Primary event research, including settlement context when available.
        rules_context = f" Settlement rules: {rules[:200]}" if rules else ""
        query1 = f"{title} Current market price: {yes_bid}¢ YES.{rules_context} What is the actual probability?"
        result1 = self.research_market(query1)
        if result1 and "Research unavailable" not in result1:
            findings.append(result1)

        # Query 2: Use only for markets where freshness matters most.
        needs_second_query = (
            config.MAX_RESEARCH_QUERIES > 1
            and rules
            and len(rules) > 20
            and (market.get("hours_to_close", 999) <= 6 or market.get("volume", 0) >= 1000)
        )
        if needs_second_query:
            query2 = f"Current data for: {rules[:200]}. What is the latest value/status?"
            result2 = self.research_market(query2)
            if result2 and "Research unavailable" not in result2:
                findings.append(result2)

        # Score quality (skip 3rd query to reduce API costs)
        sources_count = len(findings)
        if sources_count >= 3 and not contradictions:
            research_quality = "high"
        elif sources_count >= 2:
            research_quality = "medium"
        else:
            research_quality = "low"

        summary = "\n\n".join(findings[:3])
        if contradictions:
            summary += "\n\nCONTRADICTING EVIDENCE:\n" + "\n".join(contradictions)

        return {
            "findings": findings,
            "sources_count": sources_count,
            "contradictions": contradictions,
            "research_quality": research_quality,
            "summary": summary[:1500],
        }


# Keep backward-compatible alias
GeminiTradingAgent = TradingAgent


def build_cycle_prompt(
    portfolio_state: dict,
    markets: list,
    positions: list,
    settlements: list = None,
    recent_trades: list = None,
    notes: list = None,
    previous_reasoning: str = None,
    cycle_number: int = 0,
    research: dict = None,
    learning_context: str = None,
) -> str:
    """Build the dynamic user prompt for a trading cycle."""

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""## Trading Cycle #{cycle_number}
Date/Time: {now}

## Current Portfolio State
- Cash Balance: ${portfolio_state.get('cash_balance', 0) / 100:.2f}
- Trading Budget: ${portfolio_state.get('trading_budget', portfolio_state.get('cash_balance', 0)) / 100:.2f} (MAX you can spend this cycle)
- Account Value: ${portfolio_state.get('total_account_value', 0) / 100:.2f}
- Unrealized PnL: ${portfolio_state.get('unrealized_pnl', 0) / 100:.2f}
- Open Positions: {portfolio_state.get('open_positions', 0)}
- Cycle Spend So Far: ${portfolio_state.get('cycle_spend', 0) / 100:.2f}

IMPORTANT: Your total spending across ALL trades this cycle must NOT exceed the Trading Budget above.
Prefer markets that are closing SOON (within 24 hours). These have the best liquidity and fastest resolution.

POSITION SIZING: Propose a reasonable count, but final sizing is enforced in code:
- Normal trades target 6-12% of available cash, up to 16% only for very high edge.
- Buy more contracts, not just 1-2. If price is 30¢ and you want to bet $9, buy 30 contracts.
- Speculative bets are capped at ${config.SPECULATIVE_BET_MAX_DOLLARS:.0f}.

SPECULATIVE BET: You may make ONE speculative trade per day with a lower edge threshold ({config.SPECULATIVE_EDGE_MIN_CENTS}¢), capped at ${config.SPECULATIVE_BET_MAX_DOLLARS:.0f} max. Mark it with "speculative": true.

TRADE FREQUENCY: Prefer fewer high-conviction trades over forcing activity. Passing is correct when edge is unclear.

## Active Positions
"""

    if positions:
        blocked_events = set()
        for pos in positions:
            pnl = pos.get("quantity", 0) * (pos.get("current_bid", 0) - pos.get("entry_price", 0))
            prompt += (
                f"- {pos['ticker']}: {pos['quantity']} {pos['side'].upper()} "
                f"@ {pos['entry_price']}¢ (bid: {pos.get('current_bid', '?')}¢, "
                f"PnL: {pnl:+}¢)\n"
            )
            # Track event prefixes we already hold
            ticker = pos.get("ticker", "")
            if ticker.count("-") >= 2:
                blocked_events.add("-".join(ticker.split("-")[:2]))

        if blocked_events:
            prompt += f"\n⚠️ DO NOT trade any of these events (already holding positions): {', '.join(sorted(blocked_events))}\n"
    else:
        prompt += "- No open positions\n"

    prompt += "\n## Available Markets (diversified across categories, then sorted by timing/liquidity)\n"
    for market in markets[:config.MARKETS_PER_CYCLE]:
        yes_bid = market.get("yes_bid", market.get("yes_price", "?"))
        yes_ask = market.get("yes_ask", "?")
        no_bid = market.get("no_bid", market.get("no_price", "?"))
        spread = market.get("spread", "?")
        volume = market.get("volume", 0)
        days_to_close = market.get("days_to_close")
        close_info = f"{days_to_close:.1f} days" if days_to_close else market.get('close_time', 'Unknown')[:16]
        prompt += (
            f"- {market['ticker']}: \"{market.get('title', 'Unknown')}\"\n"
            f"  YES bid/ask: {yes_bid}¢/{yes_ask}¢ | NO bid/ask: {no_bid}¢/{market.get('no_ask', '?')}¢ | "
            f"Spread: {spread}¢ | Vol24h: ${volume:.0f} | "
            f"Closes: {close_info}\n"
        )
        if market.get("rules"):
            prompt += f"  Rules: {market['rules'][:150]}\n"

    if settlements:
        prompt += "\n## Recent Settlements (last 10)\n"
        for s in settlements[:10]:
            prompt += f"- {s.get('ticker', '?')}: {s.get('outcome', '?')} | PnL: {s.get('pnl', 0):+}¢\n"

    if recent_trades:
        prompt += "\n## Recent Trades (last 10)\n"
        for t in recent_trades[:10]:
            prompt += (
                f"- {t.get('ticker', '?')}: {t.get('side', '?').upper()} "
                f"{t.get('count', 0)}x @ {t.get('price', 0)}¢\n"
            )

    if notes:
        prompt += "\n## Agent Notes & Memory\n"
        for note in notes[:10]:
            prompt += f"- [{note.get('category', 'general')}] {note.get('title', '')}: {note.get('content', '')[:200]}\n"

    if previous_reasoning:
        prompt += f"\n## Previous Cycle Reasoning\n{previous_reasoning[:500]}\n"

    if research:
        prompt += "\n## Research Findings (from web search)\n"
        for ticker, findings in research.items():
            prompt += f"\n### {ticker}\n{findings[:500]}\n"

    if learning_context:
        prompt += learning_context

    prompt += """
## Trading Protocol (MANDATORY)
1. SCAN: Review all markets, identify 3-4 with potential edge
2. ANALYZE: For each opportunity, estimate true probability using research findings
3. CALCULATE: Edge = |your_probability - market_price|. Only trade if edge ≥ 7¢
4. SIZE: Suggest contract counts, knowing the execution layer will resize to risk limits.
5. EXECUTE: Place trade(s) OR explicitly PASS with reasoning

You can place MULTIPLE trades in one cycle across different market groups. Spread your budget.
The total of all trades must not exceed your Trading Budget.

CRITICAL RULES:
- Do NOT trade without research findings for that specific ticker in this cycle's Research section.
- NEVER bet on commodity/gas/oil/crypto bracket markets (KXAAAGAS*, KXBRENT*, KXWTI*, KXCOPPER*, *15M).
- NEVER bet on multiple brackets for the same event. Pick ONE bracket that matches the forecast and commit.
- NEVER repeat a trade you already made. Check Active Positions above.
- MANDATORY DIVERSIFICATION: Do not default to NBA/NHL just because they are familiar.
- If several markets have similar edge, prefer the category where the portfolio has fewer open positions.
- Do NOT propose trades with an executable ASK price below {config.MIN_TRADE_PRICE_CENTS}¢.
- If only {config.MIN_MARKETS_TO_TRADE} or fewer markets are available, PASS — do not force a trade.
- Your YES probability for a contract must be plausible: never claim >40% on contracts priced below 25¢.
- For sports/esports: the market price IS useful information. Only disagree with strong matchup-specific evidence.

PRICING: Use the ASK price (not bid) to ensure immediate fill.

Remember: Passing is a valid and often correct decision. Only trade with clear edge.

""" + RESPONSE_FORMAT

    # Append strategy context from ModelBound if available
    strategy = modelbound_skills.get_strategy_context()
    if strategy:
        prompt += f"\n## Strategy Reference (from knowledge base)\n{strategy[:800]}\n"

    return prompt
