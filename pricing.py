"""Pricing engine: determines fair value and recommended spread for markets."""

import logging
from dataclasses import dataclass
from typing import Optional

from api_client import KalshiClient
from config import PricingConfig

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    """Recommended quote parameters for a market."""

    ticker: str
    fair_value: float  # in cents, e.g., 50.0
    spread_width: int  # total spread in cents
    yes_bid: int  # recommended YES bid price in cents
    yes_ask: int  # recommended YES ask price in cents (= NO bid complement)
    confidence: float  # 0-1, how confident we are in the fair value


class PricingEngine:
    """Determines fair value and quote parameters from orderbook data."""

    def __init__(self, client: KalshiClient, config: PricingConfig):
        self.client = client
        self.config = config

    def price_market(
        self,
        ticker: str,
        inventory_skew: float = 0.0,
    ) -> Optional[Quote]:
        """Calculate fair value and recommended quotes for a market.

        Args:
            ticker: Market ticker
            inventory_skew: Cents to shift fair value by (positive = skew offer up
                to reduce long position, negative = skew bid down)

        Returns:
            Quote with recommended prices, or None if unable to price.
        """
        try:
            book = self.client.get_orderbook(ticker)
        except Exception as e:
            logger.warning("Failed to get orderbook for %s: %s", ticker, e)
            return None

        fair_value = self._compute_fair_value(book)
        if fair_value is None:
            logger.debug("Cannot determine fair value for %s", ticker)
            return None

        confidence = self._compute_confidence(book)
        spread = self._determine_spread(confidence)

        # Apply inventory skew: shift the midpoint to discourage adding to position
        skewed_mid = fair_value + inventory_skew

        # Calculate bid/ask
        half_spread = spread / 2.0
        yes_bid = int(round(skewed_mid - half_spread))
        yes_ask = int(round(skewed_mid + half_spread))

        # Enforce price bounds
        yes_bid = max(self.config.min_price, min(self.config.max_price, yes_bid))
        yes_ask = max(self.config.min_price, min(self.config.max_price, yes_ask))

        # Ensure bid < ask
        if yes_bid >= yes_ask:
            if yes_bid > self.config.min_price:
                yes_bid = yes_ask - 1
            else:
                yes_ask = yes_bid + 1

        # Final bounds check
        if yes_bid < self.config.min_price or yes_ask > self.config.max_price:
            logger.debug("Quotes for %s outside price bounds, skipping", ticker)
            return None

        return Quote(
            ticker=ticker,
            fair_value=fair_value,
            spread_width=yes_ask - yes_bid,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            confidence=confidence,
        )

    def _compute_fair_value(self, book: dict) -> Optional[float]:
        """Compute fair value from the orderbook.

        v1: Simple midpoint between best YES bid and best YES ask.
        The orderbook has YES bids and NO bids.
        Best YES ask = 100 - best NO bid.
        """
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

        best_yes_bid = None
        best_yes_ask = None

        if yes_levels:
            best_yes_bid = max(lvl[0] for lvl in yes_levels)
        if no_levels:
            best_no_bid = max(lvl[0] for lvl in no_levels)
            best_yes_ask = 100 - best_no_bid

        if best_yes_bid is not None and best_yes_ask is not None:
            return (best_yes_bid + best_yes_ask) / 2.0

        # Fallback: if only one side has quotes, use it with a wide spread assumption
        if best_yes_bid is not None:
            return best_yes_bid + self.config.default_spread_cents / 2.0
        if best_yes_ask is not None:
            return best_yes_ask - self.config.default_spread_cents / 2.0

        return None

    def _compute_confidence(self, book: dict) -> float:
        """Estimate confidence in fair value based on book depth.

        Returns a value between 0 and 1. More depth on both sides = higher confidence.
        """
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

        yes_depth = sum(lvl[1] for lvl in yes_levels) if yes_levels else 0
        no_depth = sum(lvl[1] for lvl in no_levels) if no_levels else 0

        # Both sides having depth is important
        min_depth = min(yes_depth, no_depth)
        total_depth = yes_depth + no_depth

        if total_depth == 0:
            return 0.0

        # Balanced books get higher confidence
        balance = min_depth / max(yes_depth, no_depth) if max(yes_depth, no_depth) > 0 else 0
        depth_score = min(total_depth / 200, 1.0)  # saturates at 200 total contracts

        return 0.5 * balance + 0.5 * depth_score

    def _determine_spread(self, confidence: float) -> int:
        """Determine spread width based on confidence. Lower confidence = wider spread."""
        spread_range = self.config.max_spread_cents - self.config.min_spread_cents
        # High confidence → narrow spread, low confidence → wide spread
        spread = self.config.max_spread_cents - int(round(confidence * spread_range))
        return max(self.config.min_spread_cents, min(self.config.max_spread_cents, spread))
