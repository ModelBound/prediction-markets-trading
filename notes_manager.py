"""Agent notes and memory management across trading cycles."""
import json
import os
import logging
from datetime import datetime

import config

logger = logging.getLogger(__name__)

NOTES_FILE = "data/agent_notes.json"


class NotesManager:
    """Manages persistent notes/memory for the trading agent."""

    def __init__(self):
        self.notes = self._load_notes()

    def _load_notes(self) -> list:
        if os.path.exists(NOTES_FILE):
            with open(NOTES_FILE, "r") as f:
                return json.load(f)
        return []

    def _save_notes(self):
        os.makedirs(os.path.dirname(NOTES_FILE), exist_ok=True)
        with open(NOTES_FILE, "w") as f:
            json.dump(self.notes, f, indent=2)

    def add_note(self, category: str, title: str, content: str, priority: str = "normal"):
        """Add a new note. Prunes if at capacity."""
        note = {
            "id": f"note_{len(self.notes) + 1}_{int(datetime.utcnow().timestamp())}",
            "category": category,
            "title": title[:100],
            "content": content[:config.MAX_NOTE_LENGTH],
            "priority": priority,
            "created_at": datetime.utcnow().isoformat(),
            "last_accessed": datetime.utcnow().isoformat(),
            "access_count": 0,
        }
        self.notes.append(note)
        self._prune()
        self._save_notes()
        logger.info(f"Note added: [{category}] {title}")

    def search(self, query: str = None, category: str = None) -> list:
        """Search notes by query text and/or category."""
        results = self.notes
        if category:
            results = [n for n in results if n["category"] == category]
        if query:
            query_lower = query.lower()
            results = [
                n for n in results
                if query_lower in n["content"].lower() or query_lower in n["title"].lower()
            ]

        # Update access timestamps
        for note in results:
            note["last_accessed"] = datetime.utcnow().isoformat()
            note["access_count"] = note.get("access_count", 0) + 1

        self._save_notes()
        return sorted(results, key=lambda n: n["last_accessed"], reverse=True)

    def get_recent(self, limit: int = 10) -> list:
        """Get most recent notes."""
        return sorted(self.notes, key=lambda n: n["created_at"], reverse=True)[:limit]

    def get_by_category(self, category: str) -> list:
        """Get all notes in a category."""
        return [n for n in self.notes if n["category"] == category]

    def update_from_cycle(self, notes_update: str):
        """Process notes update from agent's cycle decision."""
        if not notes_update or notes_update.strip() == "":
            return

        # Add as a general insight note
        self.add_note(
            category="cycle_insight",
            title=f"Cycle insight {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            content=notes_update,
            priority="normal",
        )

    def _prune(self):
        """Remove least valuable notes when at capacity."""
        if len(self.notes) <= config.MAX_NOTES:
            return

        # Score notes by recency and access frequency
        now = datetime.utcnow()
        scored = []
        for note in self.notes:
            try:
                last_access = datetime.fromisoformat(note["last_accessed"])
                days_since = (now - last_access).days + 1
            except (ValueError, KeyError):
                days_since = 30

            recency_score = 1.0 / days_since
            frequency_score = note.get("access_count", 0) / 10.0
            priority_bonus = 2.0 if note.get("priority") == "high" else 1.0

            # Learning notes get 3x bonus to survive pruning
            category = note.get("category", "")
            if category in ("winning_pattern", "losing_pattern"):
                priority_bonus *= 3.0
            elif category == "cycle_insight":
                priority_bonus *= 0.5  # Cycle insights pruned first

            score = (recency_score + frequency_score) * priority_bonus
            scored.append((score, note))

        scored.sort(key=lambda x: x[0], reverse=True)
        self.notes = [note for _, note in scored[:config.MAX_NOTES]]
        logger.info(f"Pruned notes to {len(self.notes)}")

    def get_critical_learning(self) -> dict:
        """Get structured learning for the agent's cycle prompt."""
        winning = self.get_by_category("winning_pattern")
        losing = self.get_by_category("losing_pattern")
        alerts = self.get_by_category("risk_alert")
        insights = self.get_by_category("cycle_insight")

        return {
            "winning_patterns": [n["content"] for n in winning[-3:]],
            "losing_patterns": [n["content"] for n in losing[-3:]],
            "risk_alerts": [n["content"] for n in alerts[-3:]],
            "recent_insights": [n["content"] for n in insights[-5:]],
        }
