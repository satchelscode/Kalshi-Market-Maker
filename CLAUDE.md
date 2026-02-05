# Kalshi Market Maker — Project Context

## What This Is
An automated market making bot for Kalshi prediction markets. It posts resting limit orders on both YES and NO sides of binary markets, collects the bid-ask spread when both fill, and manages inventory risk.

## Tech Stack
- Python 3.11+
- `requests` for REST API, `cryptography` for RSA-PSS auth, `pyyaml` for config
- No web framework — this is a long-running background process

## Module Layout (all files in repo root)
| File | Role |
|------|------|
| `config.yaml` | All tunable parameters |
| `config.py` | Dataclass config loader (YAML + env var overrides) |
| `api_client.py` | Kalshi REST client — RSA-PSS auth, rate limiting, retries |
| `scanner.py` | Finds viable markets by volume/spread/OI |
| `pricing.py` | Fair value from orderbook midpoint, confidence-based spreads |
| `quoter.py` | Order lifecycle — create YES bids + NO bids, requote on drift |
| `risk.py` | Position tracking, per-market + portfolio limits, inventory skew, kill switch |
| `main.py` | Entry point — orchestration loop, signal handlers, graceful shutdown |

## Kalshi API Essentials
- **Base URLs**: `https://demo-api.kalshi.co/trade-api/v2` (demo), `https://api.elections.kalshi.com/trade-api/v2` (prod)
- **Auth**: RSA-PSS signatures. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- **Rate limits**: 20 reads/sec, 10 writes/sec (Basic tier)
- **Orderbook**: Only returns bids. A YES bid at 48¢ = NO ask at 52¢. A NO bid at 48¢ = YES ask at 52¢.
- **Fee structure**: Taker fee `0.07 * C * P * (1-P)`. Many markets have zero maker fees — that's our edge.

## Deployment (Render)
- Deployed as a **Background Worker** on Render (not a web service)
- **Branch**: Render deploys from `main`. Feature branches merge into `main` via PR.
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `python main.py`
- **Logs**: Render Dashboard → service → "Logs" tab
- Environment variables on Render:
  - `KALSHI_API_KEY_ID` — API key ID from Kalshi dashboard
  - `KALSHI_PRIVATE_KEY` — full PEM contents of private key (paste the whole key including BEGIN/END lines)
  - `KALSHI_ENVIRONMENT` — `demo` or `prod` (start with `demo`)
- The bot reads the private key from `KALSHI_PRIVATE_KEY` env var directly (no file needed on Render)
- The code auto-detects PEM contents vs file paths — either `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` will work with raw PEM contents
- Alternatively for local dev: set `KALSHI_PRIVATE_KEY_PATH` pointing to a `.pem` file
- **Troubleshooting**: If build fails, check that Render is deploying from `main` and the merge is complete
- **Security**: Never log private key contents. If a key is leaked in logs, regenerate immediately in Kalshi dashboard

## Key Design Decisions
- **Demo-first**: Defaults to demo API. Must explicitly set `environment: "prod"` to trade real money.
- **Graceful shutdown**: SIGINT/SIGTERM handlers cancel all resting orders before exit.
- **Inventory skew**: When long YES, shifts fair value up → makes ask more attractive, bid less aggressive.
- **UUID dedup**: Every order gets a `client_order_id` for safe retries.
- **Cancel-and-replace**: v1 cancels stale orders and places new ones (vs amending in place).

## Common Tasks
- **Add a new market data source**: Extend `PricingEngine._compute_fair_value()` in `pricing.py`
- **Change quoting behavior**: Modify `QuotingEngine.update_quotes()` in `quoter.py`
- **Adjust risk limits**: Edit `config.yaml` under `risk:` section
- **Add WebSocket support**: Would replace REST polling in the main loop — currently v2 roadmap
- **Run locally**: `python main.py` (reads `config.yaml` from current directory)

## Things To Watch Out For
- Never deploy to prod without thorough demo testing
- The orderbook only returns bids — keep the YES/NO bid/ask conversion straight
- Always use `client_order_id` on orders for deduplication
- Log files go to `logs/market_maker.log` — directory is auto-created
