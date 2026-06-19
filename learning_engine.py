"""Post-settlement learning engine - analyzes outcomes and stores lessons."""
import json
import logging
from datetime import datetime

from openai import OpenAI

import config
import openai_budget
from notes_manager import NotesManager

logger = logging.getLogger(__name__)

LEARNING_PROMPT = """You are analyzing a prediction market trade that has settled. Your job is to extract a concise, actionable lesson that will help improve future predictions.

Original Trade:
- Market: {title}
- Ticker: {ticker}
- Side: {side} @ {price}¢
- AI Probability Estimate: {ai_probability}%
- Market Implied Probability: {price}%

Settlement:
- Outcome: {outcome}
- Result: {result}
- PnL: {pnl_cents}¢

Settlement Rules: {rules}

Analyze what happened. If the prediction was CORRECT, identify what signals or reasoning led to the right call. If WRONG, identify what was missed or what assumption was flawed.

Respond with JSON only:
{{
  "category": "winning_pattern" or "losing_pattern",
  "market_type": "weather" or "sports" or "crypto" or "commodities" or "politics" or "economics" or "other",
  "lesson": "A concise lesson (max 200 words) about what to remember for similar future markets",
  "key_signal": "The single most important signal that determined the outcome"
}}"""


class LearningEngine:
    """Generates post-mortem analyses after settlements and stores lessons."""

    def __init__(self, notes_manager: NotesManager):
        self.client = OpenAI(api_key=config.OPENAI_KEY)
        self.model = "gpt-4o-mini"
        self.notes = notes_manager

    def analyze_settlement(self, prediction: dict) -> dict | None:
        """
        Generate a post-mortem analysis for a settled prediction.
        Returns analysis dict or None if analysis fails.
        """
        result = prediction.get("result")
        if not result:
            return None

        if not openai_budget.can_spend("learning"):
            logger.info(f"Skipping learning analysis for {prediction.get('ticker')} due to OpenAI budget guard")
            return None

        prompt = LEARNING_PROMPT.format(
            title=prediction.get("title", "Unknown"),
            ticker=prediction.get("ticker", ""),
            side=prediction.get("side", ""),
            price=prediction.get("price", 0),
            ai_probability=prediction.get("ai_probability", 0),
            outcome=prediction.get("outcome", result),
            result=result.upper(),
            pnl_cents=prediction.get("pnl_cents", 0),
            rules=prediction.get("rules", "Not available"),
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=512,
                response_format={"type": "json_object"},
            )

            analysis = json.loads(response.choices[0].message.content.strip())
            openai_budget.record_spend("learning")
            logger.info(f"Learning analysis for {prediction.get('ticker')}: {analysis.get('category')}")
            return analysis

        except Exception as e:
            logger.warning(f"Learning analysis failed: {e}")
            return None

    def _lesson_key(self, prediction: dict) -> str:
        """Group repeated contracts/brackets into one market-event lesson."""
        ticker = prediction.get("ticker", "")
        if ticker.count("-") >= 2:
            return "-".join(ticker.split("-")[:2])
        return ticker

    def _has_existing_lesson(self, lesson_key: str, ticker: str) -> bool:
        """Avoid creating duplicate lessons for the same market/event."""
        for note in self.notes.notes:
            if note.get("category") not in ("winning_pattern", "losing_pattern"):
                continue
            title = note.get("title", "")
            content = note.get("content", "")
            if lesson_key and (lesson_key in title or f"Lesson key: {lesson_key}" in content):
                return True
            # Backward-compatible check for lessons created before lesson keys existed.
            if ticker and ticker in title:
                return True
        return False

    def store_lesson(self, analysis: dict, prediction: dict):
        """Store the lesson in the notes system."""
        if not analysis:
            return

        category = analysis.get("category", "cycle_insight")
        market_type = analysis.get("market_type", "unknown")
        lesson = analysis.get("lesson", "")
        key_signal = analysis.get("key_signal", "")
        lesson_key = self._lesson_key(prediction)

        title = f"{category}: {market_type} - {lesson_key}"
        content = f"{lesson}\nKey signal: {key_signal}\nLesson key: {lesson_key}"

        self.notes.add_note(
            category=category,
            title=title[:100],
            content=content[:config.MAX_NOTE_LENGTH],
            priority="high",
        )
        logger.info(f"Lesson stored: [{category}] {title[:50]}")

    def _aggregate_by_event(self, resolved_predictions: list) -> list:
        """Collapse repeated contracts/brackets to one representative settlement."""
        grouped = {}
        for pred in resolved_predictions:
            key = self._lesson_key(pred)
            if key not in grouped:
                grouped[key] = {**pred, "lesson_key": key, "settlement_count": 1}
            else:
                grouped[key]["settlement_count"] += 1
                grouped[key]["pnl_cents"] = grouped[key].get("pnl_cents", 0) + pred.get("pnl_cents", 0)
        return list(grouped.values())

    def process_new_settlements(self, resolved_predictions: list) -> int:
        """Analyze newly resolved predictions and store at most one lesson per event."""
        grouped_predictions = self._aggregate_by_event(resolved_predictions)
        duplicate_count = len(resolved_predictions) - len(grouped_predictions)
        if duplicate_count > 0:
            logger.info(f"Collapsed {duplicate_count} duplicate settlement predictions before learning")

        lessons_created = 0
        for pred in grouped_predictions:
            ticker = pred.get("ticker", "")
            lesson_key = self._lesson_key(pred)
            if self._has_existing_lesson(lesson_key, ticker):
                logger.info(f"Skipping duplicate lesson for {lesson_key}")
                continue
            analysis = self.analyze_settlement(pred)
            if analysis:
                self.store_lesson(analysis, pred)
                lessons_created += 1
        return lessons_created

    def get_learning_context(self) -> str:
        """Build a learning context string for the cycle prompt."""
        winning = self.notes.get_by_category("winning_pattern")[-3:]
        losing = self.notes.get_by_category("losing_pattern")[-3:]

        if not winning and not losing:
            return ""

        context = "\n## Lessons Learned (from past settlements)\n"

        if winning:
            context += "\n### What's worked:\n"
            for note in winning:
                context += f"- {note.get('content', '')[:200]}\n"

        if losing:
            context += "\n### What to avoid:\n"
            for note in losing:
                context += f"- {note.get('content', '')[:200]}\n"

        return context
