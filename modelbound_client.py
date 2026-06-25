"""ModelBound cloud client for pulling skills into the trading agent cache.

Uses the hosted ModelBound MCP endpoint (JSON-RPC over HTTPS). Requires a
personal API key in `.env` — never commit keys or synced skill content.

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
IDE_SKILL_DIRS = (".modelbound", ".kiro/skills")


def get_api_token() -> str | None:
    """Return ModelBound API token from env (supports both common var names)."""
    return config.MODELBOUND_API_TOKEN or config.MODELBOUND_API_KEY


def _parse_frontmatter_name(content: str) -> str | None:
    """Extract skill name from YAML frontmatter if present."""
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
    """Derive cache key from a local skill filename."""
    stem = filename.replace(".md", "").replace(".mdc", "")
    title = stem.replace("-", " ").replace("_", " ")
    return skill_name_to_cache_key(title)


def load_skills_from_workspace() -> dict[str, str]:
    """
    Load skills written by the ModelBound IDE extension into .modelbound/.

    This is the primary local path — no sync script or API call required when
    the extension is installed and skills are pulled to the workspace.
    """
    skills: dict[str, str] = {}
    for directory in IDE_SKILL_DIRS:
        if not os.path.isdir(directory):
            continue
        for fname in sorted(os.listdir(directory)):
            if not (fname.endswith(".md") or fname.endswith(".mdc")):
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                logger.warning(f"Could not read {path}: {e}")
                continue

            name = _parse_frontmatter_name(content) or fname
            key = skill_name_to_cache_key(name) if name != fname else _filename_to_cache_key(fname)
            skills[key] = content
            logger.debug(f"Loaded IDE skill: {fname} -> {key}")

            # Aliases the agent expects
            if key == "trading_agent_system_prompt":
                skills.setdefault("system_prompt", content)
            if key == "market_analysis":
                skills.setdefault("trading_strategy", content)
                skills.setdefault("strategy", content)
            if key == "portfolio_risk":
                skills.setdefault("portfolio_management", content)

    if skills:
        logger.info(f"Loaded {len(skills)} skills from IDE workspace ({', '.join(IDE_SKILL_DIRS)})")
    return skills


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


class ModelBoundClient:
    """Thin client for ModelBound MCP tools used by sync_skills.py."""

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
        """List skills from the ModelBound library."""
        args: dict = {"limit": limit or config.MODELBOUND_SKILL_LIMIT}
        if query:
            args["query"] = query
        result = self._call_native("list_skills", args)
        skills = self._json_result(result)
        if not isinstance(skills, list):
            return []

        if repo:
            repo_lower = repo.lower()
            skills = [
                s for s in skills
                if (s.get("repo") or "").lower() == repo_lower
            ]
        return skills

    def get_skill_body(self, skill_id: str) -> str:
        """Fetch full skill markdown/content by ID."""
        result = self._call_native("get_skill", {"skill_id": skill_id})
        text = self._text_result(result)
        if not text or text.startswith("Error"):
            raise RuntimeError(f"Failed to fetch skill {skill_id}: {text[:200]}")
        return text

    def pull_skills_for_repo(self, repo: str | None = None) -> dict[str, str]:
        """
        Pull all skills for a repo and return cache_key -> markdown content.

        Also adds alias keys the agent looks up (system_prompt, trading_strategy, etc.).
        """
        repo = repo or config.MODELBOUND_SKILL_REPO
        skills = self.list_skills(repo=repo, limit=config.MODELBOUND_SKILL_LIMIT)
        if not skills:
            skills = self.list_skills(query="kalshi trading", limit=config.MODELBOUND_SKILL_LIMIT)
            if repo:
                skills = [s for s in skills if (s.get("repo") or "").lower() == repo.lower()]

        if not skills:
            raise RuntimeError(
                f"No ModelBound skills found for repo '{repo}'. "
                "Check MODELBOUND_SKILL_REPO or run with a different query."
            )

        cache: dict[str, str] = {}
        for skill in skills:
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
            logger.info(f"Pulled skill: {name} -> {key} ({len(body)} chars)")

            # Common aliases used by modelbound_skills.py lookups
            if key == "trading_agent_system_prompt":
                cache.setdefault("system_prompt", body)
            if key == "market_analysis":
                cache.setdefault("trading_strategy", body)
                cache.setdefault("strategy", body)
            if key == "portfolio_risk":
                cache.setdefault("portfolio_management", body)

        if not cache:
            raise RuntimeError("ModelBound returned skills but none could be fetched")

        cache[META_KEY] = json.dumps({
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "modelbound_api",
            "repo": repo,
            "skill_count": len([k for k in cache if k != META_KEY]),
            "skill_keys": sorted(k for k in cache if k != META_KEY),
        })
        return cache
