"""Kalshi API client with RSA-PSS authentication and rate limiting."""

import base64
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import ApiConfig

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def acquire(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens < 1:
            wait = (1 - self.tokens) / self.rate
            logger.debug("Rate limited, sleeping %.3fs", wait)
            time.sleep(wait)
            self.tokens = 0
            self.last_refill = time.monotonic()
        else:
            self.tokens -= 1


class KalshiClient:
    """REST client for the Kalshi trading API."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self.base_url = config.base_url
        self.session = requests.Session()
        self._private_key = None
        self._read_limiter = RateLimiter(rate=20, capacity=20)
        self._write_limiter = RateLimiter(rate=10, capacity=10)

    def _load_private_key(self):
        if self._private_key is None:
            with open(self.config.private_key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(
                    f.read(), password=None
                )
        return self._private_key

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Generate RSA-PSS signature for Kalshi API auth."""
        key = self._load_private_key()
        message = f"{timestamp}{method}{path}".encode()
        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        signature = self._sign_request(method.upper(), path, timestamp)
        return {
            "KALSHI-ACCESS-KEY": self.config.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        is_write: bool = False,
        retries: int = 3,
    ) -> dict[str, Any]:
        """Make an authenticated API request with rate limiting and retries."""
        if is_write:
            self._write_limiter.acquire()
        else:
            self._read_limiter.acquire()

        path = f"/trade-api/v2{endpoint}"
        url = f"{self.base_url}{endpoint}"
        headers = self._auth_headers(method.upper(), path)

        for attempt in range(retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=10,
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("Rate limited (429), retrying in %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
            except requests.exceptions.RequestException as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning("Request failed (%s), retrying in %ds", e, wait)
                    time.sleep(wait)
                else:
                    logger.error("Request failed after %d retries: %s", retries, e)
                    raise

    # ---- Market Data ----

    def get_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict:
        """List markets with optional filters."""
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/markets", params=params)

    def get_market(self, ticker: str) -> dict:
        """Get a single market by ticker."""
        return self._request("GET", f"/markets/{ticker}")

    def get_orderbook(self, ticker: str) -> dict:
        """Get the full order book for a market."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

    def get_market_history(
        self, ticker: str, limit: int = 100, cursor: Optional[str] = None
    ) -> dict:
        """Get trade history for a market."""
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"/markets/{ticker}/trades", params=params)

    # ---- Orders ----

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price: int,
        count: int,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
        expiration_time: Optional[str] = None,
    ) -> dict:
        """Place a limit order.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            action: "buy" or "sell"
            price: Price in cents (1-99). Interpreted as yes_price if side=yes, no_price if side=no.
            count: Number of contracts
            order_type: "limit" or "market"
            client_order_id: UUID for deduplication
            expiration_time: ISO format expiration time
        """
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())

        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": order_type,
            "count": count,
            "client_order_id": client_order_id,
        }

        if side == "yes":
            body["yes_price"] = price
        else:
            body["no_price"] = price

        if expiration_time:
            body["expiration_time"] = expiration_time

        logger.info(
            "Creating order: %s %s %s %s @ %d¢ x%d",
            ticker, action, side, order_type, price, count,
        )
        return self._request("POST", "/orders", json_body=body, is_write=True)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting order."""
        logger.info("Canceling order: %s", order_id)
        return self._request("DELETE", f"/orders/{order_id}", is_write=True)

    def amend_order(
        self,
        order_id: str,
        price: Optional[int] = None,
        count: Optional[int] = None,
    ) -> dict:
        """Amend a resting order's price or count."""
        body = {}
        if price is not None:
            body["price"] = price
        if count is not None:
            body["count"] = count
        logger.info("Amending order %s: %s", order_id, body)
        return self._request(
            "PUT", f"/orders/{order_id}/amend", json_body=body, is_write=True
        )

    def batch_create_orders(self, orders: list[dict]) -> dict:
        """Batch create multiple orders."""
        logger.info("Batch creating %d orders", len(orders))
        return self._request(
            "POST", "/orders/batches", json_body={"orders": orders}, is_write=True
        )

    def batch_cancel_orders(self, order_ids: list[str]) -> dict:
        """Batch cancel multiple orders."""
        logger.info("Batch canceling %d orders", len(order_ids))
        return self._request(
            "DELETE",
            "/orders/batches",
            json_body={"order_ids": order_ids},
            is_write=True,
        )

    # ---- Portfolio ----

    def get_orders(
        self,
        ticker: Optional[str] = None,
        status: str = "resting",
    ) -> dict:
        """Get your orders, optionally filtered by ticker and status."""
        params = {"status": status}
        if ticker:
            params["ticker"] = ticker
        return self._request("GET", "/portfolio/orders", params=params)

    def get_positions(self, ticker: Optional[str] = None) -> dict:
        """Get your current positions."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        return self._request("GET", "/portfolio/positions", params=params)

    def get_balance(self) -> dict:
        """Get account balance."""
        return self._request("GET", "/portfolio/balance")

    # ---- Utilities ----

    def cancel_all_orders(self) -> list[str]:
        """Cancel all resting orders. Returns list of canceled order IDs."""
        canceled = []
        response = self.get_orders(status="resting")
        orders = response.get("orders", [])
        if not orders:
            logger.info("No resting orders to cancel")
            return canceled

        order_ids = [o["order_id"] for o in orders]
        # Batch cancel in groups of 20
        for i in range(0, len(order_ids), 20):
            batch = order_ids[i : i + 20]
            try:
                self.batch_cancel_orders(batch)
                canceled.extend(batch)
            except Exception as e:
                logger.error("Failed to batch cancel orders: %s", e)
                # Fall back to individual cancels
                for oid in batch:
                    try:
                        self.cancel_order(oid)
                        canceled.append(oid)
                    except Exception as e2:
                        logger.error("Failed to cancel order %s: %s", oid, e2)

        logger.info("Canceled %d orders", len(canceled))
        return canceled
