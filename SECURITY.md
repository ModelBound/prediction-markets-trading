# Security Policy

## Reporting Security Issues

Please report security issues privately to the maintainers instead of opening a
public issue with exploitable details.

## Secret Handling

Never commit:

- `.env` or `.env.*` files other than `.env.example`
- API tokens
- Kalshi RSA private keys
- Polymarket private keys
- GitHub personal access tokens
- DigitalOcean tokens
- Runtime files in `data/`
- Logs, local caches, or screenshots containing credentials

If a secret is exposed, rotate it immediately with the provider.

## Trading Safety

This project can place real-money trades when configured for production. Use
`TRADING_MODE=demo` by default, review risk controls before production use, and
test provider/strategy changes with small bankrolls.
