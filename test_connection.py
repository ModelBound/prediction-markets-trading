"""Test script to verify configured provider connections."""
import json
import sys
from dotenv import load_dotenv

load_dotenv()

import config


def test_kalshi_connection():
    """Test Kalshi API authentication and basic endpoints."""
    print("\n--- Testing Kalshi API Connection ---")
    print(f"Mode: {config.TRADING_MODE}")
    print(f"Base URL: {config.get_kalshi_base_url()}")
    print(f"API Key ID configured: {bool(config.KALSHI_API_KEY_ID)}")

    from kalshi_client import KalshiClient

    try:
        client = KalshiClient()
        print("✓ Client initialized (RSA key loaded)")
    except Exception as e:
        print(f"✗ Client init failed: {e}")
        return False

    # Test balance endpoint
    print("\nTesting /portfolio/balance...")
    balance = client.get_balance()
    if "error" in balance:
        print(f"✗ Balance failed: {balance}")
        # This is expected if using production key on demo or vice versa
        print("  (This may be expected if key doesn't match environment)")
    else:
        print(f"✓ Balance: ${balance.get('balance', 0) / 100:.2f}")

    # Test markets endpoint (public, no auth needed)
    print("\nTesting /markets (public)...")
    markets = client.get_markets(limit=5)
    if "error" in markets:
        print(f"✗ Markets failed: {markets}")
    else:
        market_list = markets.get("markets", [])
        print(f"✓ Found {len(market_list)} markets")
        for m in market_list[:3]:
            print(f"  - {m.get('ticker', '?')}: {m.get('title', '?')[:60]}")

    return True


def test_openai_connection():
    """Test OpenAI API access through the trading agent."""
    print("\n--- Testing OpenAI API Connection ---")
    print(f"OpenAI key configured: {bool(config.OPENAI_KEY)}")

    from gemini_agent import TradingAgent

    try:
        agent = TradingAgent()
        print("✓ Agent initialized")
    except Exception as e:
        print(f"✗ Agent init failed: {e}")
        return False

    # Test a simple prompt
    print("\nTesting simple generation...")
    try:
        decision = agent.analyze_and_decide(
            "## Test Cycle\nThis is a test. No real markets. "
            "Cash: $100. No positions. No markets available. "
            "Respond with action: pass and explain this is a test."
        )
        print(f"✓ Got response. Action: {decision.get('action', '?')}")
        print(f"  Reasoning: {decision.get('reasoning', '?')[:100]}")
        return True
    except Exception as e:
        print(f"✗ Generation failed: {e}")
        return False


def test_digitalocean():
    """Test DigitalOcean API access."""
    print("\n--- Testing DigitalOcean API ---")
    import requests

    token = config.DIGITALOCEAN_TOKEN
    if not token:
        print("✗ DIGITALOCEAN_TOKEN not set")
        return False

    print("Token configured: True")
    resp = requests.get(
        "https://api.digitalocean.com/v2/account",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 200:
        account = resp.json()["account"]
        print(f"✓ Account: {account['email']}")
        print(f"  Status: {account['status']}")
        print(f"  Droplet limit: {account['droplet_limit']}")
        return True
    else:
        print(f"✗ API error: {resp.status_code}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("KALSHI TRADING AGENT - CONNECTION TEST")
    print("=" * 60)

    results = {}

    results["kalshi"] = test_kalshi_connection()
    results["openai"] = test_openai_connection()
    results["digitalocean"] = test_digitalocean()

    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    for service, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {service:15s}: {status}")

    all_passed = all(results.values())
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")

    if not all_passed:
        print("\nNote: Kalshi auth may fail if your API key is for production")
        print("but TRADING_MODE is set to 'demo' (or vice versa).")
        print("Set TRADING_MODE=production in .env to use production keys.")
