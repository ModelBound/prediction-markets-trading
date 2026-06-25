"""Configuration for the Kalshi Trading Agent."""
import os
from dotenv import load_dotenv

load_dotenv()

# Kalshi API
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")

# Load RSA key - single-line format with \n escape sequences
_raw_rsa = os.getenv("KALSHI_API_RSA")
if _raw_rsa:
    # Replace literal \n with actual newlines (from single-line .env format)
    KALSHI_API_RSA = _raw_rsa.replace("\\n", "\n")
else:
    KALSHI_API_RSA = None
KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"  # Production
KALSHI_DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"  # Demo

# Prediction market providers
PREDICTION_MARKET_PROVIDER = os.getenv("PREDICTION_MARKET_PROVIDER", "kalshi")
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS")

# LLM
GEMINI_TOKEN = os.getenv("GEMINI_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_KEY")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai")
MODELBOUND_API_TOKEN = os.getenv("MODELBOUND_API_TOKEN")

# DigitalOcean
DIGITALOCEAN_TOKEN = os.getenv("DIGITALOCEAN_TOKEN")

# Trading Parameters
TRADING_MODE = os.getenv("TRADING_MODE", "demo")  # "demo" or "production"
CYCLE_INTERVAL_SECONDS = 1200  # 20 minutes
STARTING_BALANCE_CENTS = 1000_00  # $1000 starting (conservative)
FAST_CYCLE_INTERVAL_SECONDS = 300  # 5 minutes when markets are near close

# Market horizon: only trade markets closing within this many days
MAX_DAYS_TO_CLOSE = 3

# Risk Parameters
MAX_CONCENTRATION_PCT = 0.15  # 15% max in single market
MAX_CYCLE_SPEND_PCT = 0.25  # 25% max spend per cycle
MIN_EDGE_CENTS = 7  # Minimum 7¢ edge to trade
KELLY_FRACTION = 0.25  # Quarter-Kelly for safety
MAX_CATEGORY_EXPOSURE_PCT = 0.30  # 30% max in one category
SPECULATIVE_BET_MAX_DOLLARS = 3.00  # Max $3 on one speculative trade per day
SPECULATIVE_EDGE_MIN_CENTS = 5  # Lower edge threshold for speculative bets
MIN_TRADE_SPEND_PCT = 0.06  # Target at least 6% of available cash for normal trades
MAX_TRADE_SPEND_PCT = 0.12  # Target at most 12% of available cash for normal trades
HIGH_EDGE_TRADE_SPEND_PCT = 0.16  # Allow larger sizing for very strong edges
MIN_TRADE_PRICE_CENTS = 5  # Avoid 1-4¢ longshots unless explicitly whitelisted in code
AUTO_EXECUTE_REVIEWER_REDIRECTS = False  # Redirects are suggestions only; never auto-trade them
TARGET_DAILY_EXECUTED_TRADES = 4  # Quality over volume; was 8 and encouraged forced trades.

# Fees
TRADING_FEE_PCT = 0.048  # ~4.8%
SETTLEMENT_FEE_PCT = 0.014  # ~1.4%

# Notes
MAX_NOTES = 50
MAX_NOTE_LENGTH = 1200

# Reviewer Agent
REVIEWER_ENABLED = True
REVIEWER_MODEL = "gpt-4o-mini"

# Research — "free" uses Agent-Reach backends (Jina Reader + DuckDuckGo/Exa); "openai" uses web_search_preview
RESEARCH_PROVIDER = os.getenv("RESEARCH_PROVIDER", "free")

# Exa semantic search via mcporter (https://github.com/steipete/mcporter + https://mcp.exa.ai/mcp)
# No API key required for the hosted Exa MCP endpoint. Requires Node.js + mcporter in PATH.
EXA_MCP_URL = os.getenv(
    "EXA_MCP_URL",
    "https://mcp.exa.ai/mcp?tools=web_search_exa",
)
EXA_SEARCH_ENABLED = os.getenv("EXA_SEARCH_ENABLED", "true").lower() in ("1", "true", "yes")

# Research Thoroughness
MIN_RESEARCH_QUERIES = 1
MAX_RESEARCH_QUERIES = 1
LOW_CONFIDENCE_EDGE_PENALTY = 5  # cents to subtract from edge when research is weak
RESEARCH_INTERVAL_CYCLES = 8  # Every ~2.7 hours at the default cycle interval
RESEARCH_CACHE_TTL_HOURS = 4
MAX_RESEARCH_MARKETS = 1
MIN_RESEARCH_VOLUME = 250
REJECTION_COOLDOWN_CYCLES = 12  # Suppress repeatedly bad/rejected tickers for ~4 hours

# Series the agent must never trade (15-min crypto markets behave like random walks)
BLOCKED_TRADE_SERIES = {
    "KXSOL15M",
    "KXDOGE15M",
    "KXBTC15M",
    "KXETH15M",
}

# Minimum AI probability for low-price YES bets (avoids 6-17¢ longshots with 20-30% claimed edge)
MIN_AI_PROB_FOR_LOW_PRICE_YES = 40  # when price <= LOW_PRICE_YES_THRESHOLD_CENTS
LOW_PRICE_YES_THRESHOLD_CENTS = 20

# OpenAI cost controls. These are conservative estimates used for a daily guardrail.
# Target around $1/day in normal operation, with an absolute safety ceiling in code.
OPENAI_DAILY_BUDGET_DOLLARS = 1.00
OPENAI_HARD_DAILY_CAP_DOLLARS = 2.00
OPENAI_DECISION_COST_ESTIMATE = 0.0025
OPENAI_REVIEW_COST_ESTIMATE = 0.001
OPENAI_LEARNING_COST_ESTIMATE = 0.001
OPENAI_RESEARCH_COST_ESTIMATE = 0.030

# Market feed diversity
MARKETS_PER_CYCLE = 36
MAX_MARKETS_PER_SERIES = 2
MAX_MARKETS_PER_GROUP = 8

# Learning
LEARNING_NOTE_PRIORITY = "high"

def get_kalshi_base_url():
    """Return the appropriate base URL based on trading mode."""
    if TRADING_MODE == "production":
        return KALSHI_BASE_URL
    return KALSHI_DEMO_URL
