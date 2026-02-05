"""Risk manager: tracks inventory, enforces position limits, and manages P/L."""

import logging
from dataclasses import dataclass, field

from api_client import KalshiClient
from config import RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Position state for a single market."""

    ticker: str
    net_yes_contracts: int = 0  # positive = long YES, negative = short YES (long NO)
    total_yes_bought: int = 0
    total_no_bought: int = 0
    cost_basis_cents: float = 0.0  # total cents spent
    realized_pnl_cents: float = 0.0


@dataclass
class PortfolioState:
    """Aggregate portfolio risk state."""

    positions: dict[str, Position] = field(default_factory=dict)
    total_realized_pnl_cents: float = 0.0


class RiskManager:
    """Tracks inventory, enforces limits, and computes skew adjustments."""

    def __init__(self, client: KalshiClient, config: RiskConfig):
        self.client = client
        self.config = config
        self.portfolio = PortfolioState()
        self._kill_switch_active = False

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    def record_fill(
        self,
        ticker: str,
        side: str,
        action: str,
        price: int,
        count: int,
    ):
        """Record a fill and update position tracking.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" or "sell"
            price: Fill price in cents
            count: Number of contracts filled
        """
        if ticker not in self.portfolio.positions:
            self.portfolio.positions[ticker] = Position(ticker=ticker)

        pos = self.portfolio.positions[ticker]

        if side == "yes" and action == "buy":
            pos.net_yes_contracts += count
            pos.total_yes_bought += count
            pos.cost_basis_cents += price * count
            logger.info(
                "FILL: Bought %d YES %s @ %d¢ | Net position: %d",
                count, ticker, price, pos.net_yes_contracts,
            )
        elif side == "no" and action == "buy":
            # Buying NO is economically short YES
            pos.net_yes_contracts -= count
            pos.total_no_bought += count
            pos.cost_basis_cents += price * count
            logger.info(
                "FILL: Bought %d NO %s @ %d¢ | Net position: %d YES",
                count, ticker, price, pos.net_yes_contracts,
            )

        # Check for completed round trips
        # If we bought YES at X and NO at Y, and X + Y < 100, we locked in profit
        if pos.total_yes_bought > 0 and pos.total_no_bought > 0:
            round_trips = min(pos.total_yes_bought, pos.total_no_bought)
            if round_trips > 0:
                # Approximate: realized pnl is hard to track exactly without
                # order-level matching. We'll rely on sync_positions for accuracy.
                pass

    def get_inventory_skew(self, ticker: str) -> float:
        """Calculate the pricing skew to apply based on inventory.

        Returns cents to shift the fair value by. Positive means shift up
        (makes YES more expensive to discourage more YES buying).
        """
        pos = self.portfolio.positions.get(ticker)
        if pos is None:
            return 0.0

        # If we're long YES, shift mid up to make our YES ask cheaper (more attractive)
        # and our YES bid less aggressive, reducing the chance of adding more YES
        skew = pos.net_yes_contracts * self.config.skew_cents_per_contract
        return skew

    def check_position_limit(self, ticker: str) -> bool:
        """Check if we're within position limits for a market.

        Returns True if we can continue quoting, False if we should stop.
        """
        pos = self.portfolio.positions.get(ticker)
        if pos is None:
            return True

        if abs(pos.net_yes_contracts) >= self.config.max_position_per_market:
            logger.warning(
                "Position limit reached for %s: %d contracts",
                ticker, pos.net_yes_contracts,
            )
            return False
        return True

    def check_total_exposure(self) -> bool:
        """Check if total portfolio exposure is within limits.

        Returns True if within limits, False if we should reduce exposure.
        """
        total_exposure_cents = sum(
            abs(p.net_yes_contracts) * 100  # max loss per contract is 100¢
            for p in self.portfolio.positions.values()
        )
        total_exposure_dollars = total_exposure_cents / 100.0

        if total_exposure_dollars >= self.config.max_total_exposure_dollars:
            logger.warning(
                "Total exposure limit reached: $%.2f >= $%.2f",
                total_exposure_dollars, self.config.max_total_exposure_dollars,
            )
            return False
        return True

    def activate_kill_switch(self):
        """Activate kill switch - signal to cancel all orders."""
        logger.critical("KILL SWITCH ACTIVATED")
        self._kill_switch_active = True

    def deactivate_kill_switch(self):
        """Deactivate kill switch."""
        logger.info("Kill switch deactivated")
        self._kill_switch_active = False

    def sync_positions(self):
        """Sync positions with the exchange to correct any drift."""
        try:
            resp = self.client.get_positions()
            exchange_positions = resp.get("market_positions", [])
        except Exception as e:
            logger.error("Failed to sync positions: %s", e)
            return

        # Build a set of tickers we see on the exchange
        seen_tickers = set()

        for ep in exchange_positions:
            ticker = ep.get("ticker", "")
            seen_tickers.add(ticker)

            # Kalshi positions report YES contracts held
            yes_contracts = ep.get("position", 0)

            if ticker not in self.portfolio.positions:
                self.portfolio.positions[ticker] = Position(ticker=ticker)

            pos = self.portfolio.positions[ticker]
            if pos.net_yes_contracts != yes_contracts:
                logger.info(
                    "Position sync for %s: local=%d, exchange=%d",
                    ticker, pos.net_yes_contracts, yes_contracts,
                )
                pos.net_yes_contracts = yes_contracts

        # Clear positions for tickers no longer on exchange
        for ticker in list(self.portfolio.positions.keys()):
            if ticker not in seen_tickers:
                pos = self.portfolio.positions[ticker]
                if pos.net_yes_contracts != 0:
                    logger.info(
                        "Clearing stale position for %s (was %d)",
                        ticker, pos.net_yes_contracts,
                    )
                    pos.net_yes_contracts = 0

    def get_portfolio_summary(self) -> dict:
        """Get a summary of the current portfolio state."""
        positions = {}
        total_exposure_cents = 0

        for ticker, pos in self.portfolio.positions.items():
            if pos.net_yes_contracts == 0 and pos.total_yes_bought == 0:
                continue
            positions[ticker] = {
                "net_yes": pos.net_yes_contracts,
                "yes_bought": pos.total_yes_bought,
                "no_bought": pos.total_no_bought,
                "cost_basis_cents": pos.cost_basis_cents,
            }
            total_exposure_cents += abs(pos.net_yes_contracts) * 100

        return {
            "positions": positions,
            "total_exposure_dollars": total_exposure_cents / 100.0,
            "max_exposure_dollars": self.config.max_total_exposure_dollars,
            "realized_pnl_cents": self.portfolio.total_realized_pnl_cents,
            "kill_switch_active": self._kill_switch_active,
        }
