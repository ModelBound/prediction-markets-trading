"""ModelBound integration - loads skills from cached ModelBound content.

Skill sources (in priority order at startup):
1. Local workspace files — `.cursor/rules/`, `.kiro/skills/`, `.modelbound/` (from API sync or IDE extension)
2. `data/modelbound_cache.json` — deployed cache for headless/droplet runs
3. ModelBound API — syncs to workspace + cache when token is set and skills are missing/stale

Run `python sync_skills.py` once to pull skills from ModelBound using your API token.
The script detects your IDE layout and writes to the correct paths automatically.
"""
import json
import logging
import os
from datetime import datetime, timezone

import config
from modelbound_client import META_KEY, load_skills_from_workspace

logger = logging.getLogger(__name__)

_cache_file = "data/modelbound_cache.json"
_skill_cache: dict[str, str] = {}


def _strip_sync_markers(content: str) -> str:
    lines = content.split("\n")
    lines = [line for line in lines if not line.startswith("<!-- modelbound:")]
    return "\n".join(lines).strip()


def _load_cache_file():
    global _skill_cache
    if not os.path.exists(_cache_file):
        return
    try:
        with open(_cache_file, encoding="utf-8") as f:
            raw = json.load(f)
        loaded = {k: v for k, v in raw.items() if k != META_KEY and isinstance(v, str)}
        _skill_cache.update(loaded)
        meta = raw.get(META_KEY)
        if meta:
            try:
                info = json.loads(meta) if isinstance(meta, str) else meta
                logger.info(
                    f"Loaded {len(loaded)} skills from cache file "
                    f"(synced {info.get('synced_at', '?')})"
                )
            except (json.JSONDecodeError, TypeError):
                logger.info(f"Loaded {len(loaded)} skills from cache file")
    except Exception as e:
        logger.warning(f"Failed to load ModelBound cache file: {e}")


def _merge_workspace_skills():
    global _skill_cache
    workspace = load_skills_from_workspace()
    if workspace:
        _skill_cache.update(workspace)


def _cache_age_hours() -> float | None:
    if not os.path.exists(_cache_file):
        return None
    try:
        with open(_cache_file, encoding="utf-8") as f:
            raw = json.load(f)
        meta_raw = raw.get(META_KEY)
        if not meta_raw:
            return None
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        synced_at = meta.get("synced_at")
        if not synced_at:
            return None
        synced = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - synced).total_seconds() / 3600
    except Exception:
        return None


def _maybe_auto_sync():
    """Sync from ModelBound API when token is set and local skills are missing/stale."""
    from modelbound_client import ModelBoundClient, get_api_token

    if not get_api_token():
        return

    workspace = load_skills_from_workspace()
    age = _cache_age_hours()
    max_age = config.MODELBOUND_SYNC_MAX_AGE_HOURS

    if workspace and not config.MODELBOUND_AUTO_SYNC:
        return
    if workspace and age is not None and age < max_age:
        return

    try:
        reason = "no local skills" if not workspace else f"cache {age:.1f}h old"
        logger.info(f"Syncing ModelBound skills from API ({reason})...")
        client = ModelBoundClient()
        pulled = client.sync_skills_to_workspace()

        global _skill_cache
        _skill_cache.update({k: v for k, v in pulled.items() if k != META_KEY})

        os.makedirs(os.path.dirname(_cache_file) or ".", exist_ok=True)
        with open(_cache_file, "w", encoding="utf-8") as f:
            json.dump(pulled, f, indent=2)
    except Exception as e:
        logger.warning(f"ModelBound API sync skipped: {e}")


def load_skill(skill_key: str) -> str | None:
    _load_all()
    content = _skill_cache.get(skill_key)
    return _strip_sync_markers(content) if content else None


def _load_all():
    global _skill_cache
    if _skill_cache:
        return
    _maybe_auto_sync()
    _load_cache_file()
    _merge_workspace_skills()


def get_system_prompt() -> str | None:
    return load_skill("trading_agent_system_prompt") or load_skill("system_prompt")


def get_market_analysis_framework() -> str:
    return load_skill("market_analysis") or load_skill("trading_strategy") or ""


def get_strategy_context() -> str:
    return (
        load_skill("trading_strategy")
        or load_skill("strategy")
        or load_skill("market_analysis")
        or ""
    )


def get_reviewer_context(market_type: str) -> str:
    parts = []
    for key in ("portfolio_risk", "portfolio_management", "market_analysis"):
        content = load_skill(key)
        if content:
            parts.append(content[:500])
    return "\n\n".join(parts)


def warm_cache():
    """Pre-load skills on startup."""
    global _skill_cache
    _skill_cache = {}
    _load_all()
    if load_skills_from_workspace():
        source = "workspace (.modelbound/ + IDE paths)"
    elif _skill_cache:
        source = "cache/API"
    else:
        source = "hardcoded defaults"
    logger.info(f"ModelBound: {len(_skill_cache)} skills available (source={source})")
