"""Tests for ModelBound skill key mapping."""
import unittest

from modelbound_client import META_KEY, skill_name_to_cache_key


class TestSkillKeyMapping(unittest.TestCase):
    def test_system_prompt(self):
        self.assertEqual(
            skill_name_to_cache_key("Prediction Market Trading Agent - System Prompt"),
            "trading_agent_system_prompt",
        )

    def test_market_analysis(self):
        self.assertEqual(
            skill_name_to_cache_key("Market Analysis & Research Skill"),
            "market_analysis",
        )

    def test_portfolio_risk(self):
        self.assertEqual(
            skill_name_to_cache_key("Portfolio Management & Risk Control"),
            "portfolio_risk",
        )

    def test_generic_slug(self):
        self.assertEqual(
            skill_name_to_cache_key("Kalshi Trading Operations"),
            "kalshi_trading_operations",
        )


class TestMetaKey(unittest.TestCase):
    def test_meta_key_constant(self):
        self.assertEqual(META_KEY, "_meta")


if __name__ == "__main__":
    unittest.main()
