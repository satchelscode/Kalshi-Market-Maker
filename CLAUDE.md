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
- **Auth**: RSA-PSS signatures (SHA256, salt_length=**DIGEST_LENGTH** — not MAX_LENGTH). Sign `timestamp_ms + METHOD + /trade-api/v2/path` (no query params). Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- **Timestamp**: Use `int(time.time() * 1000)` (milliseconds). Must re-sign on each retry — stale timestamps cause 401s.
- **Rate limits**: 20 reads/sec, 10 writes/sec (Basic tier)
- **Orderbook**: Response is nested: `{"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}`. Must unwrap with `.get("orderbook", resp)`. Only returns bids. A YES bid at 48¢ = NO ask at 52¢. A NO bid at 48¢ = YES ask at 52¢.
- **Order endpoints**: All under `/portfolio/` prefix:
  - `POST /portfolio/orders` — create order
  - `DELETE /portfolio/orders/{id}` — cancel order
  - `PUT /portfolio/orders/{id}/amend` — amend order
  - `POST /portfolio/orders/batched` — batch create
  - `DELETE /portfolio/orders/batched` — batch cancel
  - `GET /portfolio/orders` — list orders
  - `GET /portfolio/positions` — list positions
  - `GET /portfolio/balance` — get balance
- **Fee structure**: Taker fee `0.07 * C * P * (1-P)`. Many markets have zero maker fees — that's our edge.
- **Market structure**: Moneyline markets have separate tickers per team (e.g., `KXNBAGAME-25FEB04-TOR-v-DET-TOR` for Toronto). Each ticker is binary YES/NO. Totals and spreads are single binary YES/NO markets.

## Target Markets
Currently targeting NCAA basketball and NBA:
- `KXNCAAMBGAME` — NCAA Men's basketball moneyline
- `KXNCAAMBSPREAD` — NCAA Men's basketball spread
- `KXNCAAMBTOTAL` — NCAA Men's basketball total points
- `KXNCAAWBGAME` — NCAA Women's basketball moneyline
- `KXNCAAWBSPREAD` — NCAA Women's basketball spread
- `KXNCAAWBTOTAL` — NCAA Women's basketball total points
- `KXNBAGAME` — NBA moneyline (high volume)

## Conservative Risk Limits (Current Prod Config)
- `order_size: 1` — 1 contract per order
- `max_position_per_market: 3` — max 3 net contracts per market
- `max_total_exposure_dollars: 10` — ~$10 max loss if everything goes south
- `max_markets: 3` — quote up to 3 markets simultaneously

## Deployment (Render)
- Deployed as a **Background Worker** on Render (not a web service)
- **Memory limit**: 512MB on Starter plan — scanner must cap pages and prefilter to avoid OOM
- **Branch**: Render deploys from `main`. Feature branches merge into `main` via PR.
- **Build command**: `pip install -r requirements.txt`
- **Start command**: `python main.py`
- **Logs**: Render Dashboard → service → "Logs" tab (primary debugging tool)
- Environment variables on Render:
  - `KALSHI_API_KEY_ID` — API key ID from Kalshi dashboard
  - `KALSHI_PRIVATE_KEY` — full PEM contents of private key (paste the whole key including BEGIN/END lines)
  - `KALSHI_ENVIRONMENT` — `demo` or `prod`
- The bot reads the private key from `KALSHI_PRIVATE_KEY` env var directly (no file needed on Render)
- The code auto-detects PEM contents vs file paths — either `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` will work with raw PEM contents
- **PEM handling**: Render/cloud env vars may store literal `\n` instead of newlines. Code auto-replaces `\\n` → `\n` in PEM strings.
- Alternatively for local dev: set `KALSHI_PRIVATE_KEY_PATH` pointing to a `.pem` file
- **Troubleshooting**: If build fails, check that Render is deploying from `main` and the merge is complete
- **Security**: Never log private key contents. If a key is leaked in logs, regenerate immediately in Kalshi dashboard

## Key Design Decisions
- **Prod with conservative limits**: Running on prod Kalshi (demo API keys can't always be created). Safety comes from tight position/exposure limits.
- **Graceful shutdown**: SIGINT/SIGTERM handlers cancel all resting orders before exit.
- **Inventory skew**: When long YES, shifts fair value up → makes ask more attractive, bid less aggressive.
- **UUID dedup**: Every order gets a `client_order_id` for safe retries.
- **Cancel-and-replace**: v1 cancels stale orders and places new ones (vs amending in place).
- **Memory-conscious scanning**: MAX_SCAN_PAGES=2, prefilter by volume/OI before fetching orderbooks, gc.collect() after scanning.
- **Log to console only**: `log_file: ""` in config — Render captures stdout. No file logging avoids disk issues.

## Common Tasks
- **Add a new market data source**: Extend `PricingEngine._compute_fair_value()` in `pricing.py`
- **Change quoting behavior**: Modify `QuotingEngine.update_quotes()` in `quoter.py`
- **Adjust risk limits**: Edit `config.yaml` under `risk:` section
- **Add new target series**: Add series ticker to `config.yaml` under `scanner.target_series`
- **Target specific markets**: Add tickers to `config.yaml` under `scanner.target_tickers` (overrides series scanning)
- **Add WebSocket support**: Would replace REST polling in the main loop — currently v2 roadmap
- **Run locally**: `python main.py` (reads `config.yaml` from current directory)

## Debugging Lessons Learned
- **401 Unauthorized**: Check RSA-PSS salt_length (must be DIGEST_LENGTH), timestamp freshness (re-sign on retry), and correct environment (demo vs prod keys are separate)
- **404 on orders**: Order endpoints need `/portfolio/` prefix — `/portfolio/orders`, not `/orders`
- **Orderbook empty**: API response nests data under `{"orderbook": {...}}` — must unwrap before accessing `yes`/`no` keys
- **OOM on Render**: Cap scanner pages (MAX_SCAN_PAGES=2), prefilter candidates before orderbook fetch, call gc.collect() after scanning
- **0 candidates**: Check volume/OI minimums aren't too strict; verify target_series tickers match Kalshi's naming convention
- **PEM key errors**: Render may store literal `\n` in env vars; code handles this with `.replace("\\n", "\n")`

## Things To Watch Out For
- The orderbook only returns bids — keep the YES/NO bid/ask conversion straight
- Always use `client_order_id` on orders for deduplication
- Demo and prod API keys are completely separate — a prod key won't work on demo and vice versa
- Moneyline series have separate tickers per team; each is independently quoted as a binary market
- Render's 512MB memory limit can be hit if scanning too many markets or fetching too many orderbooks
