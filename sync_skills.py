"""Sync skills from ModelBound to local cache for the agent to use at runtime.
Run this locally (where MCP is available) to populate the cache,
then deploy the cache file to the droplet.

Usage: python sync_skills.py
"""
import json
import os
import sys


def sync_from_corpus_data():
    """
    Pull skill content from ModelBound corpus and write to local cache.
    This script is meant to run in the IDE where MCP tools are available.
    It writes data/modelbound_cache.json which the agent reads at runtime.
    """
    # These are the skill contents from our corpus (pre-fetched)
    # In practice, run this from an IDE session where MCP tools pull live data
    cache_file = "data/modelbound_cache.json"

    # Check if we can read from existing .modelbound/ files
    skills = {}

    mb_dir = ".modelbound"
    if os.path.exists(mb_dir):
        for fname in os.listdir(mb_dir):
            if fname.endswith(".md"):
                key = fname.replace(".md", "").replace("-", "_")
                with open(os.path.join(mb_dir, fname), "r") as f:
                    skills[key] = f.read()
                print(f"  Loaded: {key} ({len(skills[key])} chars)")

    # Also check .kiro/skills/
    kiro_skills = ".kiro/skills"
    if os.path.exists(kiro_skills):
        for fname in os.listdir(kiro_skills):
            if fname.endswith(".md"):
                key = fname.replace(".md", "").replace("-", "_")
                if key not in skills:
                    with open(os.path.join(kiro_skills, fname), "r") as f:
                        skills[key] = f.read()
                    print(f"  Loaded: {key} ({len(skills[key])} chars)")

    if not skills:
        print("No skills found in .modelbound/ or .kiro/skills/")
        print("Create skill files there first, or use ModelBound MCP tools.")
        sys.exit(1)

    # Write cache
    os.makedirs("data", exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(skills, f, indent=2)

    print(f"\nWrote {len(skills)} skills to {cache_file}")
    print("Deploy this file to the droplet: scp data/modelbound_cache.json root@<IP>:/opt/trading-agent/data/")


if __name__ == "__main__":
    print("Syncing ModelBound skills to local cache...")
    sync_from_corpus_data()
