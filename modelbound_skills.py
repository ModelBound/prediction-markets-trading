"""ModelBound integration - loads skills and knowledge from cached ModelBound content.

Skills are synced from ModelBound to data/modelbound_cache.json via:
1. The IDE (Kiro) using MCP tools to pull content
2. The sync_skills.py script reading from .modelbound/ and .kiro/skills/

The agent reads from this cache at runtime - no direct API calls needed.
"""
import json
import logging
import os

import config

logger = logging.getLogger(__name__)

_cache_file = "data/modelbound_cache.json"
_skill_cache = {}


def _load_cache():
    """Load the skill cache from disk."""
    global _skill_cache
    if _skill_cache:
        return
    try:
        if os.path.exists(_cache_file):
            with open(_cache_file, "r") as f:
                _skill_cache = json.load(f)
            logger.info(f"Loaded {len(_skill_cache)} skills from ModelBound cache")
        else:
            logger.info("No ModelBound cache found - using hardcoded defaults")
    except Exception as e:
        logger.warning(f"Failed to load ModelBound cache: {e}")


def load_skill(skill_key: str) -> str | None:
    """Load a skill by key from the local cache."""
    _load_cache()
    content = _skill_cache.get(skill_key)
    if content:
        # Strip ModelBound sync markers
        lines = content.split("\n")
        lines = [l for l in lines if not l.startswith("<!-- modelbound:")]
        return "\n".join(lines).strip()
    return None


def get_system_prompt() -> str | None:
    """Load the trading agent system prompt from cache."""
    return load_skill("trading_agent_system_prompt") or load_skill("system_prompt")


def get_market_analysis_framework() -> str:
    """Load market analysis framework."""
    return load_skill("market_analysis") or load_skill("trading_strategy") or ""


def get_strategy_context() -> str:
    """Load current trading strategy."""
    return load_skill("trading_strategy") or load_skill("strategy") or ""


def get_reviewer_context(market_type: str) -> str:
    """Get relevant context for the reviewer based on market type."""
    # Pull from portfolio/risk skill if available
    risk = load_skill("portfolio_risk") or load_skill("portfolio_management")
    if risk:
        return risk[:500]
    return ""


def warm_cache():
    """Pre-load cache on startup."""
    _load_cache()
    logger.info(f"ModelBound cache: {len(_skill_cache)} skills available")
