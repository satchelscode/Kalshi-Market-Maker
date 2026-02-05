# Kalshi Market Maker

Automated market making bot for Kalshi prediction markets. Posts resting limit orders on both YES and NO sides of binary markets, collects the bid-ask spread when both sides fill, and manages inventory risk.

## Architecture

| Module | File | Purpose |
|--------|------|---------|
| Config | `config.py` | Loads YAML config with env var overrides |
| API Client | `api_client.py` | RSA-PSS auth, rate limiting, REST methods |
| Scanner | `scanner.py` | Identifies viable markets by volume/spread/OI |
| Pricing | `pricing.py` | Fair value from orderbook midpoint, spread sizing |
| Quoter | `quoter.py` | Order lifecycle: create, amend, cancel |
| Risk | `risk.py` | Position tracking, limits, inventory skew, kill switch |
| Main | `main.py` | Orchestration loop tying all modules together |

## Setup

```bash
pip install -r requirements.txt
```

1. Generate an API key pair in your Kalshi dashboard
2. Save the private key as a `.pem` file
3. Copy `config.yaml` and fill in your `key_id` and `private_key_path`
4. **Start with demo environment** (`environment: "demo"`)

## Usage

```bash
# Run with default config.yaml
python main.py

# Run with custom config
python main.py path/to/config.yaml

# Override via environment variables
KALSHI_API_KEY_ID=your_key KALSHI_PRIVATE_KEY_PATH=key.pem python main.py
```

## Configuration

See `config.yaml` for all options. Key settings:

- `api.environment`: `"demo"` or `"prod"` — **always start with demo**
- `scanner.target_tickers`: List specific market tickers to quote
- `pricing.default_spread_cents`: Spread width (default 4¢)
- `quoting.order_size`: Contracts per order (default 10)
- `risk.max_position_per_market`: Max net contracts per market
- `risk.max_total_exposure_dollars`: Portfolio-wide exposure cap

## How It Works

1. **Scan**: Finds high-volume markets with tradeable spreads
2. **Price**: Computes fair value from orderbook midpoint
3. **Quote**: Posts YES bids and NO bids (= YES asks) around fair value
4. **Manage**: Tracks fills, adjusts quotes based on inventory skew
5. **Protect**: Enforces position limits, total exposure cap, kill switch

The bot cancels all orders on shutdown (SIGINT/SIGTERM/crash).
