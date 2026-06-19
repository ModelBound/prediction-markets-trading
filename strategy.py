"""Strategy extension points for decision and research algorithms.

The default strategy is prompt-driven and implemented in gemini_agent.py plus
reviewer_agent.py. Contributors can use these protocols when extracting a more
modular strategy package or adding deterministic strategies.
"""
from typing import Protocol


class ResearchStrategy(Protocol):
    """Select and research markets before the decision step."""

    def research_markets(self, markets: list[dict]) -> dict[str, str]:
        ...


class DecisionStrategy(Protocol):
    """Convert market, portfolio, research, and memory context into an action."""

    def decide(self, cycle_prompt: str) -> dict:
        ...


class ReviewStrategy(Protocol):
    """Approve, reject, or redirect proposed trades before execution."""

    def review(self, trade: dict, context: dict):
        ...
