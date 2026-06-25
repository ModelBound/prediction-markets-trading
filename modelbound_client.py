"""ModelBound cloud client for pulling skills into the trading agent cache.

Uses the hosted ModelBound MCP endpoint (JSON-RPC over HTTPS). Requires a
personal API key in `.env` — never commit keys or synced skill content.

Skills are written to:
  - `.modelbound/` — canonical copy (always)
  - IDE-native paths from API metadata (e.g. `.cursor/rules/`, `.kiro/skills/`)
    when the detected IDE matches the skill's source_platform

Docs: https://modelbound.co/guides/context-api
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

import requests

import config

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "https://mcp.modelbound.co/"
META_KEY = "_meta"

# Directories scanned at runtime (recursive). Order: IDE-native first, then canonical.
WORKSPACE_SKILL_ROOTS = (
    ".cursor/rules",
    ".cursor/skills",
    ".kiro/skills",
    ".kiro/steering",
    ".claude/skills",
    ".modelbound",
)

IDE_PLATFORM_DIRS = {
    "cursor": ".cursor",
    "kiro": ".kiro",
    "claude": ".claude",
    "claude-code": ".claude",
    "copilot": ".github",
}


def get_api_token() -> str | None:
    """Return ModelBound API token from env (supports both common var names)."""
    return config.MODELBOUND_API_TOKEN or config.MODELBOUND_API_KEY


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "skill"


def _parse_frontmatter_name(content: str) -> str | None:
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    for line in parts[1].splitlines():
        if line.strip().lower().startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _filename_to_cache_key(filename: str) -> str:
    stem = os.path.basename(filename).replace(".md", "").replace(".mdc", "")
    if stem.upper() == "SKILL":
        stem = os.path.basename(os.path.dirname(filename))
    title = stem.replace("-", " ").replace("_", " ")
    return skill_name_to_cache_key(title)


def skill_name_to_cache_key(name: str, ai_type: str | None = None) -> str:
    """Map a ModelBound skill name to the cache key the agent expects."""
    lowered = (name or "").lower()
    if "system prompt" in lowered:
        return "trading_agent_system_prompt"
    if "market analysis" in lowered or "research skill" in lowered:
        return "market_analysis"
    if "portfolio" in lowered and "risk" in lowered:
        return "portfolio_risk"
    if "portfolio" in lowered:
        return "portfolio_management"
    if "trading strategy" in lowered:
        return "trading_strategy"
    if ai_type == "system-prompt":
        return "system_prompt"

    slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return slug or "skill"


def apply_skill_aliases(skills: dict[str, str]) -> None:
    """Add lookup aliases the trading agent expects."""
    if "trading_agent_system_prompt" in skills:
        skills.setdefault("system_prompt", skills["trading_agent_system_prompt"])
    if "market_analysis" in skills:
        skills.setdefault("trading_strategy", skills["market_analysis"])
        skills.setdefault("strategy", skills["market_analysis"])
    if "portfolio_risk" in skills:
        skills.setdefault("portfolio_management", skills["portfolio_risk"])


def detect_workspace_ides() -> list[str]:
    """
    Detect which IDE layouts exist locally, or use MODELBOUND_IDE override.

    Returns platform slugs matching ModelBound source_platform values.
    """
    if config.MODELBOUND_IDE and config.MODELBOUND_IDE != "auto":
        return [config.MODELBOUND_IDE]

    detected = []
    for platform, root in IDE_PLATFORM_DIRS.items():
        if os.path.isdir(root):
            if platform == "claude-code":
                detected.append("claude-code")
            elif platform not in detected:
                detected.append(platform)
    return detected


def _should_mirror_to_ide_path(source_path: str | None, platform: str | None, detected_ides: list[str]) -> bool:
    """Whether to write a skill to its IDE-native path from API metadata."""
    if not config.MODELBOUND_SYNC_IDE_PATHS or not source_path:
        return False

    root = source_path.split("/")[0]
    if root == ".cursor" and platform == "cursor":
        # Mirror Cursor rules even before .cursor/ exists (first sync creates it)
        return "cursor" in detected_ides or not detected_ides
    if root == ".kiro" and platform == "kiro":
        return "kiro" in detected_ides
    if root == ".claude" and platform in ("claude", "claude-code"):
        return "claude" in detected_ides or "claude-code" in detected_ides
    return False


def _write_skill_file(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def load_skills_from_workspace() -> dict[str, str]:
    """
    Load skills from local IDE paths and .modelbound/.

    Scans .cursor/rules, .kiro/skills (nested SKILL.md), and .modelbound/.
    """
    skills: dict[str, str] = {}
    seen_paths: set[str] = set()

    for root in WORKSPACE_SKILL_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if not (fname.endswith(".md") or fname.endswith(".mdc")):
                    continue
                path = os.path.join(dirpath, fname)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                try:
                    with open(path, encoding="utf-8") as f:
                        content = f.read()
                except OSError as e:
                    logger.warning(f"Could not read {path}: {e}")
                    continue

                name = _parse_frontmatter_name(content)
                if name:
                    key = skill_name_to_cache_key(name)
                else:
                    key = _filename_to_cache_key(path)

                # First path wins unless same key — later paths override (IDE before modelbound)
                skills[key] = content

    apply_skill_aliases(skills)
    if skills:
        logger.info(f"Loaded {len(skills)} skills from workspace paths")
    return skills


class ModelBoundClient:
    """Thin client for ModelBound MCP tools."""

    def __init__(self, api_token: str | None = None, mcp_url: str | None = None):
        self.api_token = api_token or get_api_token()
        self.mcp_url = mcp_url or config.MODELBOUND_MCP_URL or DEFAULT_MCP_URL
        if not self.api_token:
            raise ValueError(
                "ModelBound API token not configured. Set MODELBOUND_API_TOKEN "
                "(or MODELBOUND_API_KEY) in .env"
            )

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_token}",
        }

    def _call_tool(self, name: str, arguments: dict | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        response = requests.post(
            self.mcp_url,
            json=payload,
            headers=self._headers(),
            timeout=config.MODELBOUND_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message", "ModelBound MCP error"))
        return body.get("result", {})

    def _call_native(self, tool_name: str, arguments: dict | None = None) -> dict:
        return self._call_tool(
            "modelbound.callTool",
            {"tool_name": tool_name, "arguments": arguments or {}},
        )

    @staticmethod
    def _text_result(result: dict) -> str:
        content = result.get("content") or []
        if not content:
            return ""
        return content[0].get("text", "")

    @staticmethod
    def _json_result(result: dict):
        text = ModelBoundClient._text_result(result)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def list_skills(
        self,
        query: str | None = None,
        repo: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        args: dict = {"limit": limit or config.MODELBOUND_SKILL_LIMIT}
        if query:
            args["query"] = query
        result = self._call_native("list_skills", args)
        skills = self._json_result(result)
        if not isinstance(skills, list):
            return []

        if repo:
            repo_lower = repo.lower()
            skills = [s for s in skills if (s.get("repo") or "").lower() == repo_lower]
        return skills

    def get_skill_body(self, skill_id: str) -> str:
        result = self._call_native("get_skill", {"skill_id": skill_id})
        text = self._text_result(result)
        if not text or text.startswith("Error") or text == "File not found":
            raise RuntimeError(f"Failed to fetch skill {skill_id}: {text[:200]}")
        return text

    def pull_skills_for_repo(self, repo: str | None = None) -> dict[str, str]:
        """Pull skills into memory cache dict (no local files)."""
        repo = repo or config.MODELBOUND_SKILL_REPO
        skills_meta = self.list_skills(repo=repo, limit=config.MODELBOUND_SKILL_LIMIT)
        if not skills_meta:
            raise RuntimeError(f"No ModelBound skills found for repo '{repo}'")

        cache: dict[str, str] = {}
        for skill in skills_meta:
            skill_id = skill.get("id")
            name = skill.get("name", skill_id)
            if not skill_id:
                continue
            try:
                body = self.get_skill_body(skill_id)
            except Exception as e:
                logger.warning(f"Skipping skill '{name}': {e}")
                continue

            key = skill_name_to_cache_key(name, skill.get("ai_type"))
            cache[key] = body
            logger.info(f"Pulled skill: {name} -> {key}")

        apply_skill_aliases(cache)
        if not cache:
            raise RuntimeError("ModelBound returned skills but none could be fetched")

        cache[META_KEY] = json.dumps({
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "modelbound_api",
            "repo": repo,
            "skill_count": len([k for k in cache if k != META_KEY]),
            "skill_keys": sorted(k for k in cache if k != META_KEY),
            "detected_ides": detect_workspace_ides(),
        })
        return cache

    def sync_skills_to_workspace(self, repo: str | None = None) -> dict[str, str]:
        """
        Pull repo skills from ModelBound API and write to local paths.

        Always writes canonical copies to `.modelbound/`. Also mirrors to
        IDE-native paths from API metadata when the platform matches the
        detected IDE (e.g. `.cursor/rules/` for Cursor, `.kiro/skills/` for Kiro).
        """
        repo = repo or config.MODELBOUND_SKILL_REPO
        detected_ides = detect_workspace_ides()
        skills_meta = self.list_skills(repo=repo, limit=config.MODELBOUND_SKILL_LIMIT)
        if not skills_meta:
            raise RuntimeError(f"No ModelBound skills found for repo '{repo}'")

        cache: dict[str, str] = {}
        written_paths: list[str] = []

        for skill in skills_meta:
            skill_id = skill.get("id")
            name = skill.get("name", skill_id)
            platform = skill.get("source_platform")
            source_path = skill.get("source_path")
            if not skill_id:
                continue

            try:
                body = self.get_skill_body(skill_id)
            except Exception as e:
                logger.warning(f"Skipping skill '{name}': {e}")
                continue

            key = skill_name_to_cache_key(name, skill.get("ai_type"))
            cache[key] = body

            # Canonical copy — always
            canonical = os.path.join(".modelbound", f"{slugify(name)}.md")
            _write_skill_file(canonical, body)
            written_paths.append(canonical)

            # IDE-native path from API metadata
            if _should_mirror_to_ide_path(source_path, platform, detected_ides):
                _write_skill_file(source_path, body)
                written_paths.append(source_path)
                logger.info(f"Mirrored {name} -> {source_path} ({platform})")

        apply_skill_aliases(cache)
        if not cache:
            raise RuntimeError("ModelBound returned skills but none could be fetched")

        cache[META_KEY] = json.dumps({
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "modelbound_api_workspace",
            "repo": repo,
            "skill_count": len([k for k in cache if k != META_KEY]),
            "skill_keys": sorted(k for k in cache if k != META_KEY),
            "detected_ides": detected_ides,
            "written_paths": written_paths,
        })
        logger.info(
            f"Synced {len(cache) - 1} skills to workspace "
            f"(IDEs: {detected_ides or ['none']}, files: {len(written_paths)})"
        )
        return cache
