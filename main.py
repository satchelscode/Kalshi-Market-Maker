"""Main loop: orchestrates the market making bot."""

import gc
import logging
import os
import signal
import sys
import time
from pathlib import Path

from api_client import KalshiClient
from config import load_config
from pricing import PricingEngine
from quoter import QuotingEngine
from risk import RiskManager
from scanner import MarketScanner

logger = logging.getLogger("market_maker")

# Global reference for signal handlers
_bot: "MarketMakerBot | None" = None


class MarketMakerBot:
    """Main market making bot that ties all modules together."""

    def __init__(self, config_path: str | None = None):
        self.config = load_config(config_path)
        self.client = KalshiClient(self.config.api)
        self.scanner = MarketScanner(self.client, self.config.scanner)
        self.pricing = PricingEngine(self.client, self.config.pricing)
        self.quoter = QuotingEngine(self.client, self.config.quoting)
        self.risk = RiskManager(self.client, self.config.risk)
        self.running = False
        self.target_tickers: list[str] = []

    def start(self):
        """Start the market making bot."""
        self._setup_logging()
        self._register_signal_handlers()

        logger.info("=" * 60)
        logger.info("Kalshi Market Maker starting")
        logger.info("Environment: %s", self.config.api.environment)
        logger.info("Base URL: %s", self.config.api.base_url)
        logger.info("=" * 60)

        # Verify connectivity
        try:
            balance = self.client.get_balance()
            bal_cents = balance.get("balance", 0)
            logger.info("Account balance: $%.2f", bal_cents / 100.0)
        except Exception as e:
            logger.error("Failed to connect to Kalshi API: %s", e)
            logger.error("Check your API credentials and environment setting.")
            sys.exit(1)

        # Scan for target markets (retry up to 5 times with delay)
        targets = []
        for scan_attempt in range(5):
            logger.info("Scanning for markets (attempt %d)...", scan_attempt + 1)
            targets = self.scanner.scan()
            if targets:
                break
            logger.warning("No viable markets found, retrying in 30s...")
            time.sleep(30)

        if not targets:
            logger.error("No viable markets found after 5 attempts. Check scanner config.")
            sys.exit(0)

        self.target_tickers = [t.ticker for t in targets]
        for t in targets:
            logger.info(
                "Target: %s (vol=%d, OI=%d, spread=%s, score=%.1f)",
                t.ticker, t.volume_24h, t.open_interest,
                f"{t.spread}¢" if t.spread else "N/A", t.score,
            )

        # Sync existing positions
        self.risk.sync_positions()

        # Free memory from scanning
        gc.collect()

        # Main loop
        self.running = True
        logger.info("Entering main loop (poll interval: %.1fs)", self.config.loop.poll_interval_sec)

        try:
            while self.running:
                self._loop_iteration()
                time.sleep(self.config.loop.poll_interval_sec)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self._shutdown()

    def _loop_iteration(self):
        """Single iteration of the main loop."""
        # Check kill switch
        if self.risk.kill_switch_active:
            self.quoter.cancel_all_quotes()
            logger.warning("Kill switch active, all quotes canceled")
            return

        # Sync order state
        self.quoter.sync_orders()

        # Sync positions periodically (every iteration for now)
        self.risk.sync_positions()

        # Check total exposure
        if not self.risk.check_total_exposure():
            logger.warning("Total exposure limit exceeded, canceling all quotes")
            self.quoter.cancel_all_quotes()
            return

        # Process fills from the quoter
        fills = self.quoter.get_fills_since_sync()
        for fill in fills:
            self.risk.record_fill(
                ticker=fill["ticker"],
                side=fill["side"],
                action=fill["action"],
                price=fill["price"],
                count=fill["filled_count"],
            )

        # Update quotes for each target market
        for ticker in self.target_tickers:
            self._update_market(ticker)

        # Log portfolio state
        summary = self.risk.get_portfolio_summary()
        if summary["positions"]:
            logger.info(
                "Portfolio: exposure=$%.2f/%s, positions=%d",
                summary["total_exposure_dollars"],
                f"${summary['max_exposure_dollars']:.0f}",
                len(summary["positions"]),
            )

    def _update_market(self, ticker: str):
        """Update quotes for a single market."""
        # Check position limits
        if not self.risk.check_position_limit(ticker):
            # Over limit, cancel quotes for this market
            self.quoter.cancel_market_quotes(ticker)
            return

        # Get inventory skew
        skew = self.risk.get_inventory_skew(ticker)

        # Price the market
        quote = self.pricing.price_market(ticker, inventory_skew=skew)
        if quote is None:
            logger.debug("No price available for %s, skipping", ticker)
            return

        logger.debug(
            "%s: fair=%.1f¢, bid=%d¢, ask=%d¢, spread=%d¢, conf=%.2f, skew=%.1f",
            ticker, quote.fair_value, quote.yes_bid, quote.yes_ask,
            quote.spread_width, quote.confidence, skew,
        )

        # Update quotes
        self.quoter.update_quotes(quote)

    def _shutdown(self):
        """Graceful shutdown: cancel all orders."""
        logger.info("Shutting down...")
        self.running = False

        # Cancel all managed quotes
        try:
            self.quoter.cancel_all_quotes()
        except Exception as e:
            logger.error("Error canceling managed quotes: %s", e)

        # Belt and suspenders: cancel ALL resting orders via API
        try:
            canceled = self.client.cancel_all_orders()
            if canceled:
                logger.info("Emergency canceled %d additional orders", len(canceled))
        except Exception as e:
            logger.error("Error in emergency cancel: %s", e)

        # Final portfolio summary
        summary = self.risk.get_portfolio_summary()
        logger.info("Final portfolio state: %s", summary)
        logger.info("Market Maker shut down cleanly")

    def _setup_logging(self):
        """Configure logging."""
        log_level = getattr(logging, self.config.logging.level.upper(), logging.INFO)

        # Create logs directory if needed
        log_file = self.config.logging.log_file
        if log_file:
            log_dir = Path(log_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)

        # Root logger config
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
        if log_file:
            handlers.append(logging.FileHandler(log_file))

        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=handlers,
        )

    def _register_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        global _bot
        _bot = self

        def handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info("Received signal %s, initiating shutdown", sig_name)
            if _bot:
                _bot.running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    bot = MarketMakerBot(config_path)
    bot.start()


if __name__ == "__main__":
    main()
