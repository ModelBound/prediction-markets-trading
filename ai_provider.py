"""AI provider extension points.

The current trading agent uses OpenAI directly because it relies on structured
JSON responses and web search for research. Future providers should implement
this interface and be wired into TradingAgent, ReviewerAgent, and LearningEngine.
"""
from typing import Protocol


class AIProvider(Protocol):
    """Provider interface for decision, review, research, and learning calls."""

    def decision_json(self, system_prompt: str, user_prompt: str) -> dict:
        ...

    def review_json(self, system_prompt: str, user_prompt: str) -> dict:
        ...

    def research_text(self, query: str) -> str:
        ...

    def learning_json(self, prompt: str) -> dict:
        ...


def get_ai_provider_name() -> str:
    """Return the configured provider name for docs and diagnostics."""
    import config

    return config.AI_PROVIDER.lower()
