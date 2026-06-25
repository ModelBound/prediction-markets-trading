# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- ModelBound skill integration: pull skills via API (`modelbound_client.py`), load from
  `.modelbound/` IDE extension files at startup, optional `sync_skills.py` for deploy caches
- Support `MODELBOUND_API_KEY` as alias for `MODELBOUND_API_TOKEN`

### Changed

- ModelBound MCP endpoint default: `https://mcp.modelbound.co/` (was `/mcp`)
- `MODELBOUND_AUTO_SYNC` defaults to `false`; IDE extension path is primary for local dev

## [0.2.0] - 2026-06-25

### Added

- **Free web research (default)** — Market research now uses
  [Agent-Reach](https://github.com/Panniantong/Agent-Reach)-compatible backends
  instead of OpenAI `web_search_preview` (~$0.03/call):
  - **Exa** semantic search via [mcporter](https://github.com/steipete/mcporter)
    (installed in Docker; no API key required for the hosted MCP endpoint)
  - **DuckDuckGo HTML** search as a server-friendly fallback
  - **Jina Reader** for reading result pages as markdown
- `web_research.py` module and `RESEARCH_PROVIDER=free|openai` configuration
- `EXA_MCP_URL` and `EXA_SEARCH_ENABLED` environment variables
- Unit tests for Exa result parsing (`tests/test_web_research.py`)
- Trading guardrails: block 15-minute crypto series (`KXSOL15M`, `KXDOGE15M`, etc.),
  expanded weather market exclusion, and minimum AI probability for low-price YES bets
- Dashboard remote sync via `DROPLET_IP`, `DROPLET_API_PORT`, or `DROPLET_API_URL`

### Changed

- Docker image installs Node.js and `mcporter` so Exa search works out of the box on deployment
- Agent prompt de-emphasizes forced trade volume; quality over quantity
- `TARGET_DAILY_EXECUTED_TRADES` reduced from 8 to 4

### Migration

- **Default behavior:** research is free. No action required for new installs.
- **To keep paid OpenAI web search:** set `RESEARCH_PROVIDER=openai` in `.env`.
- **Dashboard users monitoring a remote agent:** set `DROPLET_IP` (or
  `DROPLET_API_URL`) in your local `.env`.

## [0.1.0] - 2026-06-01

### Added

- Initial open source release: Kalshi trading agent with OpenAI decisions, reviewer,
  settlement learning, portfolio risk controls, local dashboard, and DigitalOcean
  deployment via Docker Compose

[Unreleased]: https://github.com/ModelBound/prediction-markets-trading/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ModelBound/prediction-markets-trading/compare/e58f0cd...v0.2.0
