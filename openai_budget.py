"""Lightweight daily OpenAI cost guardrail."""
import json
import logging
import os
from datetime import datetime

import config

logger = logging.getLogger(__name__)

USAGE_FILE = "data/openai_usage.json"
USAGE_HISTORY_FILE = "data/openai_usage_history.json"

ESTIMATES = {
    "decision": config.OPENAI_DECISION_COST_ESTIMATE,
    "review": config.OPENAI_REVIEW_COST_ESTIMATE,
    "learning": config.OPENAI_LEARNING_COST_ESTIMATE,
    "research": config.OPENAI_RESEARCH_COST_ESTIMATE,
}


def _today() -> str:
    return datetime.utcnow().date().isoformat()


def _default_usage() -> dict:
    return {
        "date": _today(),
        "estimated_spend": 0.0,
        "counts": {kind: 0 for kind in ESTIMATES},
    }


def _load_usage() -> dict:
    try:
        if os.path.exists(USAGE_FILE):
            with open(USAGE_FILE, "r") as f:
                usage = json.load(f)
            if usage.get("date") == _today():
                usage.setdefault("counts", {})
                usage["estimated_spend"] = _estimated_spend_from_counts(usage["counts"])
                return usage
            _archive_usage(usage)
    except Exception as e:
        logger.warning(f"Failed to load OpenAI usage file: {e}")
    return _default_usage()


def _archive_usage(usage: dict):
    """Keep daily usage snapshots so budget tuning can use actual history."""
    usage_date = usage.get("date")
    if not usage_date:
        return

    usage = dict(usage)
    usage["estimated_spend"] = _estimated_spend_from_counts(usage.get("counts", {}))

    history = []
    try:
        if os.path.exists(USAGE_HISTORY_FILE):
            with open(USAGE_HISTORY_FILE, "r") as f:
                history = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load OpenAI usage history: {e}")
        history = []

    history = [day for day in history if day.get("date") != usage_date]
    history.append(usage)
    history = sorted(history, key=lambda day: day.get("date", ""))[-30:]

    os.makedirs(os.path.dirname(USAGE_HISTORY_FILE), exist_ok=True)
    with open(USAGE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def _estimated_spend_from_counts(counts: dict) -> float:
    """Recalculate spend so tuning estimates does not strand the agent all day."""
    return round(
        sum(ESTIMATES.get(kind, 0.0) * int(count or 0) for kind, count in counts.items()),
        6,
    )


def _save_usage(usage: dict):
    os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
    with open(USAGE_FILE, "w") as f:
        json.dump(usage, f, indent=2)


def remaining_budget() -> float:
    usage = _load_usage()
    return max(0.0, _daily_budget_limit() - float(usage.get("estimated_spend", 0.0)))


def can_spend(kind: str) -> bool:
    estimate = ESTIMATES.get(kind, 0.0)
    usage = _load_usage()
    projected = float(usage.get("estimated_spend", 0.0)) + estimate
    budget_limit = _daily_budget_limit()
    if projected <= budget_limit:
        return True
    logger.warning(
        f"OpenAI daily budget guard: skipping {kind} "
        f"(estimated ${projected:.2f} > ${budget_limit:.2f})"
    )
    return False


def record_spend(kind: str):
    usage = _load_usage()
    counts = usage.setdefault("counts", {})
    counts[kind] = counts.get(kind, 0) + 1
    usage["estimated_spend"] = _estimated_spend_from_counts(counts)
    _save_usage(usage)


def _daily_budget_limit() -> float:
    configured = float(getattr(config, "OPENAI_DAILY_BUDGET_DOLLARS", 0.0) or 0.0)
    hard_cap = float(getattr(config, "OPENAI_HARD_DAILY_CAP_DOLLARS", 2.0) or 2.0)
    return min(configured, hard_cap)
