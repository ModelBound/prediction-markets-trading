# Contributing

Thanks for helping improve this AI-powered prediction market trading platform.

Good contribution areas include:

- Prediction market provider adapters
- AI provider adapters
- Strategy, research, and reviewer improvements
- Replay/backtesting tools
- Settlement and scorecard correctness
- Documentation and deployment hardening

## Guidelines

- Keep pull requests focused and explain the trading or safety impact.
- Do not commit secrets, `.env`, private keys, runtime `data/`, logs, or local
  caches.
- Default examples to `TRADING_MODE=demo`.
- Preserve deterministic risk checks before any execution path.
- Add tests or replay evidence for strategy, execution, settlement, and PnL
  changes when possible.

## Provider Changes

For new prediction market providers, implement the protocol in
`market_provider.py` and document any required credentials in `.env.example`
and `README.md`.

For new AI providers, implement the contract described in `ai_provider.py` and
preserve structured JSON responses for decision, review, and learning steps.
