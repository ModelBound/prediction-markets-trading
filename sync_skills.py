"""Optional: build data/modelbound_cache.json for headless deployment.

You do NOT need this script if you use the ModelBound IDE extension — it writes
skills to `.modelbound/` and the agent loads them automatically at startup.

Use this script when you need a portable cache file, e.g. before deploying to a
droplet without the IDE extension:

    python sync_skills.py              # .modelbound/ first, else API
    python sync_skills.py --local      # .modelbound/ only
    python sync_skills.py --api        # ModelBound API only (for deploy bundles)

Requires MODELBOUND_API_TOKEN (or MODELBOUND_API_KEY) in .env for --api.
Never commit `.env`, API keys, or `data/modelbound_cache.json`.
"""
import argparse
import json
import os
import sys

import config
from modelbound_client import (
    META_KEY,
    ModelBoundClient,
    get_api_token,
    load_skills_from_workspace,
)


def write_cache(skills: dict[str, str], cache_file: str = "data/modelbound_cache.json") -> int:
    os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2)

    count = len([k for k in skills if k != META_KEY])
    print(f"\nWrote {count} skills to {cache_file}")
    print("Deploy to droplet: ./deploy_droplet.sh <ip>  (includes cache if present)")
    return count


def sync_from_api() -> dict[str, str]:
    if not get_api_token():
        raise RuntimeError(
            "ModelBound API token not set. Add MODELBOUND_API_TOKEN or "
            "MODELBOUND_API_KEY to .env. See .env.example."
        )
    client = ModelBoundClient()
    print(f"Pulling skills from ModelBound API (repo={config.MODELBOUND_SKILL_REPO})...")
    return client.pull_skills_for_repo()


def sync_skills(*, local_only: bool = False, api_only: bool = False) -> dict[str, str]:
    """Build deploy cache. Returns skill dict."""
    skills: dict[str, str] = {}

    if not api_only:
        skills = load_skills_from_workspace()
        if skills:
            print(f"Using {len(skills)} skills from .modelbound/ (IDE extension)")

    if local_only:
        if not skills:
            raise RuntimeError(
                "No skills in .modelbound/ or .kiro/skills/. "
                "Pull skills via the ModelBound IDE extension first."
            )
    elif api_only or not skills:
        skills = sync_from_api()

    write_cache(skills)
    return skills


def main():
    parser = argparse.ArgumentParser(
        description="Optional: build modelbound_cache.json for droplet deployment",
    )
    parser.add_argument("--local", action="store_true", help="Use .modelbound/ only")
    parser.add_argument("--api", action="store_true", help="Use ModelBound API only")
    args = parser.parse_args()

    print("Building ModelBound skill cache (optional — IDE extension users can skip this)...")
    try:
        sync_skills(local_only=args.local, api_only=args.api)
    except Exception as e:
        print(f"Sync failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
