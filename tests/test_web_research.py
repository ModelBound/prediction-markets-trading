"""Tests for free web research (Exa parsing, URL helpers)."""
import unittest

from web_research import parse_exa_text_results, _unwrap_ddg_redirect


SAMPLE_EXA_TEXT = """Title: Kalshi - Prediction Market for Trading the Future
URL: https://kalshi.com/
Published: N/A
Author: N/A
Highlights:
Kalshi is a prediction market platform.

---

Title: Example Article
URL: https://example.com/article
Highlights:
Some snippet text here.
"""


class TestExaParsing(unittest.TestCase):
    def test_parse_exa_text_results(self):
        results = parse_exa_text_results(SAMPLE_EXA_TEXT)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://kalshi.com/")
        self.assertIn("Kalshi", results[0]["title"])
        self.assertEqual(results[1]["url"], "https://example.com/article")

    def test_parse_empty(self):
        self.assertEqual(parse_exa_text_results(""), [])


class TestDuckDuckGoHelpers(unittest.TestCase):
    def test_unwrap_ddg_redirect(self):
        wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com"
        self.assertEqual(_unwrap_ddg_redirect(wrapped), "https://example.com")


if __name__ == "__main__":
    unittest.main()
