"""Sync ModelBound skills to local workspace paths and deploy cache.

Uses your ModelBound API token to discover skills for MODELBOUND_SKILL_REPO,
detect your IDE layout, and write files to the right places:

  - `.modelbound/` — canonical copy (always)
  - `.cursor/rules/` — Cursor rules (when source_platform=cursor)
  - `.kiro/skills/` — Kiro skills (when source_platform=kiro and .kiro/ exists)

Also writes `data/modelbound_cache.json` for headless/droplet deployment.

Usage:
    python sync_skills.py           # API sync to workspace + cache (default)
    python sync_skills.py --local   # read existing workspace files into cache only
    python sync_skills.py --cache   # API pull to cache only (no local files)

Requires MODELBOUND_API_TOKEN (or MODELBOUND_API_KEY) in .env for API modes.
Never commit `.env`, API keys, or synced skill content.
"""
import argparse
import json
import os
import sys

import config
from modelbound_client import META_KEY, ModelBoundClient, get_api_token, load_skills_from_workspace


def write_cache(skills: dict[str, str], cache_file: str = "data/modelbound_cache.json") -> int:
    os.makedirs(os.path.dirname(cache_file) or ".", exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(skills, f, indent=2)
    count = len([k for k in skills if k != META_KEY])
    print(f"Wrote {count} skills to {cache_file}")
    return count


def sync_skills(*, local_only: bool = False, cache_only: bool = False) -> dict[str, str]:
    if local_only:
        skills = load_skills_from_workspace()
        if not skills:
            raise RuntimeError(
                "No skills found locally. Run `python sync_skills.py` with a "
                "ModelBound API token to pull skills first."
            )
        write_cache(skills)
        return skills

    if not get_api_token():
        raise RuntimeError(
            "ModelBound API token not set. Add MODELBOUND_API_TOKEN or "
            "MODELBOUND_API_KEY to .env. See .env.example."
        )

    client = ModelBoundClient()
    from modelbound_client import detect_workspace_ides

    ides = detect_workspace_ides()
    print(f"Detected IDE layout: {ides or ['(none — will write .modelbound/ only)']}")
    print(f"Pulling skills for repo={config.MODELBOUND_SKILL_REPO}...")

    if cache_only:
        skills = client.pull_skills_for_repo()
    else:
        skills = client.sync_skills_to_workspace()

    write_cache(skills)
    print("Deploy to droplet: ./deploy_droplet.sh <ip>")
    return skills


def main():
    parser = argparse.ArgumentParser(description="Sync ModelBound skills to local workspace")
    parser.add_argument("--local", action="store_true", help="Rebuild cache from local files only")
    parser.add_argument("--cache", action="store_true", help="API pull to cache only (no workspace files)")
    args = parser.parse_args()

    try:
        sync_skills(local_only=args.local, cache_only=args.cache)
    except Exception as e:
        print(f"Sync failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
