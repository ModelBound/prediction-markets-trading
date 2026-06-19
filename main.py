"""Main entry point for the Kalshi Trading Agent."""
import os
import sys
import time
import signal
import logging
from datetime import datetime

from trading_cycle import TradingCycle
import config
import modelbound_skills
from data_api import start_data_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/trading.log"),
    ],
)
logger = logging.getLogger(__name__)

# Graceful shutdown
shutdown_requested = False


def signal_handler(signum, frame):
    global shutdown_requested
    logger.info("Shutdown signal received. Finishing current cycle...")
    shutdown_requested = True


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def validate_config():
    """Validate all required configuration is present."""
    errors = []

    market_provider = config.PREDICTION_MARKET_PROVIDER.lower()
    ai_provider = config.AI_PROVIDER.lower()

    if market_provider == "kalshi":
        if not config.KALSHI_API_KEY_ID:
            errors.append("KALSHI_API_KEY_ID not set")
        if not config.KALSHI_API_RSA:
            errors.append("KALSHI_API_RSA not set")
    elif market_provider == "polymarket":
        errors.append("Polymarket provider is a scaffold and is not production-ready yet")
    else:
        errors.append(f"Unsupported PREDICTION_MARKET_PROVIDER: {config.PREDICTION_MARKET_PROVIDER}")

    if ai_provider != "openai":
        errors.append(f"Unsupported AI_PROVIDER: {config.AI_PROVIDER}")
    if ai_provider == "openai" and not config.OPENAI_KEY:
        errors.append("OPENAI_KEY not set")

    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        sys.exit(1)

    logger.info("Configuration validated successfully")
    logger.info(f"Trading mode: {config.TRADING_MODE}")
    logger.info(f"Prediction market provider: {config.PREDICTION_MARKET_PROVIDER}")
    logger.info(f"AI provider: {config.AI_PROVIDER}")
    logger.info(f"Cycle interval: {config.CYCLE_INTERVAL_SECONDS}s")
    logger.info(f"Max concentration: {config.MAX_CONCENTRATION_PCT:.0%}")
    logger.info(f"Kelly fraction: {config.KELLY_FRACTION}")


def main():
    """Main trading loop."""
    os.makedirs("data", exist_ok=True)

    logger.info("=" * 60)
    logger.info("KALSHI PREDICTION MARKET TRADING AGENT")
    logger.info(f"Started at: {datetime.utcnow().isoformat()}")
    logger.info("=" * 60)

    validate_config()

    # Start data API for dashboard sync (replaces SSH/SCP)
    start_data_api()

    # Pre-load skills from ModelBound
    modelbound_skills.warm_cache()

    # Initialize trading cycle
    cycle = TradingCycle()

    logger.info(f"\nStarting trading loop (interval: {config.CYCLE_INTERVAL_SECONDS}s)...")
    logger.info(f"Mode: {config.TRADING_MODE.upper()}")
    logger.info("Press Ctrl+C to stop gracefully.\n")

    while not shutdown_requested:
        try:
            cycle_start = time.time()

            # Run one trading cycle
            result = cycle.run_cycle()

            # Log summary
            action = result.get("action", "unknown")
            balance = result.get("balance", 0)
            logger.info(
                f"\n--- Cycle Summary ---\n"
                f"  Action: {action}\n"
                f"  Balance: ${balance / 100:.2f}\n"
                f"  Account Value: ${result.get('account_value', 0) / 100:.2f}\n"
                f"  Duration: {result.get('duration_seconds', 0):.1f}s\n"
                f"---\n"
            )

            # Wait for next cycle - shorter if we have positions closing soon
            elapsed = time.time() - cycle_start
            base_interval = config.CYCLE_INTERVAL_SECONDS

            # Speed up only after we actually trade a market that is closing soon.
            # Short-duration markets are always in the feed, so using the feed alone
            # can accidentally create 5-minute cycles all day.
            executed_tickers = {
                t.get("ticker")
                for t in result.get("trades", [])
                if t.get("success")
            }
            has_closing_soon = any(
                m.get("ticker") in executed_tickers
                and m.get("hours_to_close", 999) < 2
                for m in result.get("markets_data", [])
            ) if executed_tickers and result.get("markets_data") else False

            if has_closing_soon:
                interval = config.FAST_CYCLE_INTERVAL_SECONDS
                logger.info("Markets closing soon - next check in 5 min")
            else:
                interval = base_interval

            wait_time = max(0, interval - elapsed)

            if wait_time > 0 and not shutdown_requested:
                logger.info(f"Next cycle in {wait_time:.0f}s...")
                # Sleep in small increments to allow graceful shutdown
                sleep_end = time.time() + wait_time
                while time.time() < sleep_end and not shutdown_requested:
                    time.sleep(min(10, sleep_end - time.time()))

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            # Wait before retrying
            time.sleep(60)

    logger.info("\nTrading agent stopped gracefully.")
    logger.info(f"Final state saved. Total cycles run: {cycle.cycle_number - 1}")


if __name__ == "__main__":
    main()
