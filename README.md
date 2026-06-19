# Prediction Markets Trading

> **Disclaimer:** This software is provided for educational and experimental
> purposes only. It is not financial advice. The authors and contributors are
> not responsible for any financial losses, damages, or other consequences
> resulting from the use of this software. Prediction markets carry real
> financial risk. You are solely responsible for your own trading decisions and
> any funds you choose to risk. Use at your own discretion.

An open source AI-powered prediction market trading platform. The default
integration trades on Kalshi with OpenAI for decisions, review, research, and
post-settlement learning. The code is structured so contributors can add other
prediction markets, AI providers, research strategies, and risk/decision
algorithms.

This project is experimental software. Prediction markets involve real financial
risk. Start in demo mode, use small bankrolls, and review every strategy before
running unattended.

## What It Does

The agent runs a repeated trading cycle:

1. Checks Kalshi settlements and updates the scorecard.
2. Refreshes cash, positions, and current account value.
3. Scans prediction markets and filters for liquid, executable contracts.
4. Gathers free research where possible and paid AI research when useful.
5. Asks the decision agent to identify mispriced markets.
6. Runs deterministic risk checks and an AI reviewer before any order.
7. Executes approved trades and records outcomes for future learning.

## Current Defaults

- Prediction market: `kalshi`
- AI provider: `openai`
- Infra target: DigitalOcean Droplet with Docker Compose
- Skill management: optional ModelBound-synced local cache
- Trading mode: `demo` unless `TRADING_MODE=production`
- Kalshi API: V2 order endpoint (`/portfolio/events/orders`)

Polymarket support is intentionally a scaffold. The extension points are present,
but a contributor must implement the Polymarket CLOB client, market
normalization, order placement, positions, and settlement reconciliation before
using `PREDICTION_MARKET_PROVIDER=polymarket`.

## Repository Structure

- `main.py` - scheduler and process entry point
- `trading_cycle.py` - market scan, research, decision, review, execution
- `kalshi_client.py` - Kalshi API client with RSA authentication
- `market_provider.py` - prediction market provider factory and protocol
- `polymarket_client.py` - placeholder adapter for future Polymarket support
- `gemini_agent.py` - OpenAI-backed decision and research agent
- `reviewer_agent.py` - second-pass AI trade reviewer
- `learning_engine.py` - post-settlement lesson generation
- `portfolio.py` - local portfolio/risk state
- `settlement_detector.py` - scorecard reconciliation from settlements
- `free_research.py` - no-cost weather research helpers
- `ai_provider.py` - AI provider extension protocol
- `strategy.py` - strategy/research/review extension protocols
- `dashboard.py` - local dashboard
- `deploy_droplet.sh` - simple DigitalOcean Droplet deployment

## Setup

Create an isolated Python environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local environment file:

```bash
cp .env.example .env
```

Fill in your own secrets in `.env`. Never commit `.env`, private keys, API
tokens, or runtime files from `data/`.

Required for the default Kalshi/OpenAI setup:

- `KALSHI_API_KEY_ID`
- `KALSHI_API_RSA`
- `OPENAI_KEY`
- `TRADING_MODE=demo`
- `PREDICTION_MARKET_PROVIDER=kalshi`
- `AI_PROVIDER=openai`

Optional:

- `MODELBOUND_API_TOKEN` if you use ModelBound for skill/prompt management
- `DIGITALOCEAN_TOKEN` and `DIGITALOCEAN_SSH_KEY_ID` for DigitalOcean helpers
- `POLYMARKET_*` values for contributors implementing Polymarket support

## Getting API Keys

Kalshi:

1. Create or log in to a Kalshi account.
2. Create API credentials in Kalshi's developer/API settings.
3. Put the key ID in `KALSHI_API_KEY_ID`.
4. Put the RSA private key in `KALSHI_API_RSA`. For `.env`, store it as one
   line with literal `\n` newline escapes.

OpenAI:

1. Create an OpenAI API key.
2. Put it in `OPENAI_KEY`.
3. Review `OPENAI_DAILY_BUDGET_DOLLARS` and
   `OPENAI_HARD_DAILY_CAP_DOLLARS` in `config.py`.

ModelBound:

1. Create your own ModelBound account/API token.
2. Store it locally as `MODELBOUND_API_TOKEN`.
3. Sync skills/prompts into `data/modelbound_cache.json` for runtime use.
4. Do not commit ModelBound tokens or private skill caches.

DigitalOcean:

1. Create a DigitalOcean API token if you want to use `deploy.py`.
2. Add your SSH key to DigitalOcean and set `DIGITALOCEAN_SSH_KEY_ID`.
3. For the simple Droplet deploy script, make sure your SSH key is available at
   `~/.ssh/id_rsa_digitalocean` or adjust the `SSH_KEY` variable at the top of
   `deploy_droplet.sh` to point to your actual key path.

## Quick Start

After completing setup and filling in `.env`:

1. Start the agent (locally or via Docker — see sections below).
2. Start the dashboard: `python dashboard.py`
3. Open `http://localhost:8888` in your browser.
4. **Activate a trading bankroll** using the control panel at the top of the
   dashboard. The agent starts in **monitor-only mode** and will not place
   trades until you explicitly set a budget. You can enter a dollar amount or
   click "Use Cash Balance" to use your current Kalshi cash.
5. Confirm the next cycle picks up the bankroll by checking the logs for
   `Bankroll: $X.XX initial | Available: ...` instead of
   `Trading INACTIVE (no bankroll set)`.

The agent will now scan markets, research, decide, review, and execute trades
every 20 minutes.

## Running Locally

Start the trading agent:

```bash
python main.py
```

Start the dashboard in another terminal:

```bash
python dashboard.py
```

Open the dashboard at `http://localhost:8888`.

## Docker

The Docker image does not copy `.env` or private keys into the image. Runtime
secrets are supplied through Docker Compose:

```bash
docker compose up -d --build
docker compose logs -f
```

The container exposes port 9090 for the data API (used by the dashboard to sync
state). The dashboard itself is **not containerized** — run it locally with
`python dashboard.py` and it will connect to the agent's data API.

## DigitalOcean Deployment

DigitalOcean is the default infra path, but the app does not depend on it. Any
host that can run Docker Compose can run the agent.

Simple Droplet deployment:

```bash
./deploy_droplet.sh <droplet_ip>
```

The script uploads source files and your local `.env` to the remote host, then
runs Docker Compose. Keep the remote `.env` protected and rotate secrets if you
ever suspect exposure.

**Important:** Do not run the agent locally AND on a remote droplet at the same
time against the same Kalshi account. Both instances share the same bankroll
state and can create duplicate orders or conflicting position tracking. Run the
agent in one place only; use the dashboard locally to monitor.

Optional DigitalOcean API helper:

```bash
python deploy.py account
python deploy.py create
python deploy.py list
```

## Extending Providers

Prediction markets:

1. Implement the `PredictionMarketClient` protocol in `market_provider.py`.
2. Normalize markets to the fields consumed by `trading_cycle.py`.
3. Implement balances, positions, orders, fills, and settlements.
4. Add the provider to `get_market_client()`.
5. Add provider-specific setup docs and tests.

AI providers:

1. Implement the interface sketched in `ai_provider.py`.
2. Preserve structured JSON outputs for decisions, review, and learning.
3. Add provider-specific cost tracking to `openai_budget.py` or a new budget
   module.
4. Update `TradingAgent`, `ReviewerAgent`, and `LearningEngine` to use the
   provider factory.

Strategies and research:

1. Use `strategy.py` as the contract for research, decision, and review modules.
2. Keep deterministic pre-flight checks in place before execution.
3. Add tests or replay scripts when changing trade selection logic.

## Risk Controls

Important defaults in `config.py`:

- `MAX_CONCENTRATION_PCT = 0.15`
- `MAX_CYCLE_SPEND_PCT = 0.25`
- `MIN_EDGE_CENTS = 7`
- `KELLY_FRACTION = 0.25`
- `MAX_CATEGORY_EXPOSURE_PCT = 0.30`
- `OPENAI_DAILY_BUDGET_DOLLARS = 1.00`
- `OPENAI_HARD_DAILY_CAP_DOLLARS = 2.00`

Risk controls are not guarantees. Market data, settlement rules, API behavior,
and model outputs can be wrong or delayed.

## Security

- Never commit `.env`, API tokens, RSA private keys, `data/`, logs, or local
  caches.
- Use demo mode by default.
- Use minimal bankrolls when testing production mode.
- Rotate any token that was ever copied into chat, logs, screenshots, or git.
- Review generated trades and settlement reporting before trusting metrics.

## Troubleshooting

**Agent says "Trading INACTIVE (no bankroll set)":**
You need to activate a trading budget through the dashboard control panel. The
agent will not trade until a bankroll is explicitly set. See the Quick Start
section.

**Orders fail with 410 error:**
You are running an outdated version of `kalshi_client.py`. Pull the latest code
which uses the current Kalshi V2 order endpoint.

**Dashboard won't start (port 8888 already in use):**
Another process is using port 8888. Kill it with `lsof -ti :8888 | xargs kill`
or change the `PORT` variable in `dashboard.py`.

**Dashboard shows "connected: false" or stale data:**
The dashboard tries to reach the agent's data API on port 9090. If the agent is
on a remote droplet, make sure port 9090 is accessible (check firewall/security
group rules). The dashboard falls back to cached local data when the API is
unreachable.

**Both local and remote agents running simultaneously:**
This causes conflicting trades and budget state corruption. Only run the trading
agent in one location. Use the dashboard locally for monitoring.

**SSH key errors with deploy_droplet.sh:**
The script expects a key at `~/.ssh/id_rsa_digitalocean`. Edit the `SSH_KEY`
variable at the top of the script to match your actual key path.

## Contributing

Contributions are welcome for:

- New market providers
- Better market normalization
- Safer execution and risk controls
- AI provider adapters
- Deterministic strategies
- Backtesting/replay tools
- Dashboard improvements
- Tests and documentation

Please keep changes small, include a clear explanation, and avoid committing
private configuration or runtime state.
