"""Quoting engine: manages resting limit orders on both sides of markets."""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from api_client import KalshiClient
from config import QuotingConfig
from pricing import Quote

logger = logging.getLogger(__name__)


@dataclass
class ManagedOrder:
    """An order we're actively managing."""

    order_id: str
    client_order_id: str
    ticker: str
    side: str  # "yes" or "no"
    action: str  # "buy"
    price: int  # in cents
    count: int
    remaining_count: int


@dataclass
class MarketQuotes:
    """Active quotes for a single market."""

    ticker: str
    yes_bid_order: Optional[ManagedOrder] = None
    no_bid_order: Optional[ManagedOrder] = None  # NO bid = YES ask


class QuotingEngine:
    """Manages the lifecycle of resting limit orders across markets."""

    def __init__(self, client: KalshiClient, config: QuotingConfig):
        self.client = client
        self.config = config
        self.active_quotes: dict[str, MarketQuotes] = {}

    def update_quotes(self, quote: Quote) -> MarketQuotes:
        """Update quotes for a market based on the pricing engine's recommendation.

        Posts YES bids at quote.yes_bid and NO bids at (100 - quote.yes_ask).
        Amends existing orders if the price has moved beyond the threshold.
        """
        ticker = quote.ticker

        if ticker not in self.active_quotes:
            self.active_quotes[ticker] = MarketQuotes(ticker=ticker)

        mq = self.active_quotes[ticker]

        # Desired prices
        desired_yes_bid = quote.yes_bid
        desired_no_bid = 100 - quote.yes_ask  # NO bid is complement of YES ask

        # Update YES bid
        mq.yes_bid_order = self._update_side(
            mq.yes_bid_order,
            ticker=ticker,
            side="yes",
            desired_price=desired_yes_bid,
        )

        # Update NO bid (which acts as our YES ask)
        mq.no_bid_order = self._update_side(
            mq.no_bid_order,
            ticker=ticker,
            side="no",
            desired_price=desired_no_bid,
        )

        return mq

    def _update_side(
        self,
        existing: Optional[ManagedOrder],
        ticker: str,
        side: str,
        desired_price: int,
    ) -> Optional[ManagedOrder]:
        """Update one side (YES bid or NO bid) of our quote."""
        # If we have an existing order, check if it needs amending
        if existing is not None:
            price_diff = abs(existing.price - desired_price)
            if price_diff < self.config.requote_threshold_cents:
                # Price hasn't moved enough, keep existing order
                return existing

            # Price has moved, cancel and replace
            # (amend could also work but cancel+replace is simpler for v1)
            try:
                self.client.cancel_order(existing.order_id)
                logger.info(
                    "Canceled stale %s bid for %s at %d¢",
                    side, ticker, existing.price,
                )
            except Exception as e:
                logger.warning("Failed to cancel order %s: %s", existing.order_id, e)

        # Place new order
        try:
            client_oid = str(uuid.uuid4())
            resp = self.client.create_order(
                ticker=ticker,
                side=side,
                action="buy",
                price=desired_price,
                count=self.config.order_size,
                order_type="limit",
                client_order_id=client_oid,
            )
            order = resp.get("order", resp)
            order_id = order.get("order_id", "")

            managed = ManagedOrder(
                order_id=order_id,
                client_order_id=client_oid,
                ticker=ticker,
                side=side,
                action="buy",
                price=desired_price,
                count=self.config.order_size,
                remaining_count=self.config.order_size,
            )
            logger.info(
                "Placed %s bid for %s at %d¢ x%d (order=%s)",
                side, ticker, desired_price, self.config.order_size, order_id,
            )
            return managed

        except Exception as e:
            logger.error("Failed to place %s bid for %s: %s", side, ticker, e)
            return None

    def cancel_market_quotes(self, ticker: str):
        """Cancel all quotes for a specific market."""
        mq = self.active_quotes.get(ticker)
        if not mq:
            return

        for order in [mq.yes_bid_order, mq.no_bid_order]:
            if order is not None:
                try:
                    self.client.cancel_order(order.order_id)
                    logger.info("Canceled %s order %s for %s", order.side, order.order_id, ticker)
                except Exception as e:
                    logger.warning("Failed to cancel order %s: %s", order.order_id, e)

        mq.yes_bid_order = None
        mq.no_bid_order = None

    def cancel_all_quotes(self):
        """Cancel all managed quotes across all markets."""
        for ticker in list(self.active_quotes.keys()):
            self.cancel_market_quotes(ticker)
        self.active_quotes.clear()

    def sync_orders(self):
        """Sync our internal state with the exchange.

        Fetches all resting orders and reconciles with our tracked orders.
        Detects fills and removes stale entries.
        """
        try:
            resp = self.client.get_orders(status="resting")
            resting = {o["order_id"]: o for o in resp.get("orders", [])}
        except Exception as e:
            logger.error("Failed to sync orders: %s", e)
            return

        for ticker, mq in self.active_quotes.items():
            for attr in ("yes_bid_order", "no_bid_order"):
                order = getattr(mq, attr)
                if order is None:
                    continue

                exchange_order = resting.get(order.order_id)
                if exchange_order is None:
                    # Order is no longer resting (filled or canceled)
                    logger.info(
                        "Order %s (%s %s for %s) no longer resting",
                        order.order_id, order.side, order.action, ticker,
                    )
                    setattr(mq, attr, None)
                else:
                    # Update remaining count
                    order.remaining_count = exchange_order.get(
                        "remaining_count", order.remaining_count
                    )

    def get_fills_since_sync(self) -> list[dict]:
        """Detect orders that were filled since last sync.

        Returns list of dicts with fill info for the risk manager.
        """
        fills = []
        for ticker, mq in self.active_quotes.items():
            for attr in ("yes_bid_order", "no_bid_order"):
                order = getattr(mq, attr)
                if order is None:
                    continue

                filled = order.count - order.remaining_count
                if filled > 0:
                    fills.append({
                        "ticker": ticker,
                        "side": order.side,
                        "action": order.action,
                        "price": order.price,
                        "filled_count": filled,
                    })
        return fills
