"""Market scanner: identifies viable markets for market making."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from api_client import KalshiClient
from config import ScannerConfig

logger = logging.getLogger(__name__)


@dataclass
class MarketInfo:
    """Summary of a market's suitability for market making."""

    ticker: str
    title: str
    volume_24h: int
    open_interest: int
    yes_bid: Optional[int]  # best YES bid in cents
    yes_ask: Optional[int]  # best YES ask in cents (= 100 - best NO bid)
    spread: Optional[int]  # ask - bid in cents
    time_to_expiry_sec: float
    has_maker_fees: bool
    score: float = 0.0  # composite ranking score


class MarketScanner:
    """Scans Kalshi markets and ranks them for market making viability."""

    def __init__(self, client: KalshiClient, config: ScannerConfig):
        self.client = client
        self.config = config

    def scan(self) -> list[MarketInfo]:
        """Scan markets and return ranked list of viable targets."""
        # If specific tickers are configured, just use those
        if self.config.target_tickers:
            return self._scan_specific_tickers(self.config.target_tickers)

        # Otherwise scan by series or all open markets
        markets = self._fetch_markets()
        viable = self._filter_markets(markets)
        ranked = self._rank_markets(viable)
        return ranked[: self.config.max_markets]

    def _fetch_markets(self) -> list[dict]:
        """Fetch all open markets, optionally filtered by series."""
        all_markets = []

        if self.config.target_series:
            for series in self.config.target_series:
                cursor = None
                while True:
                    resp = self.client.get_markets(
                        status="open", series_ticker=series, cursor=cursor
                    )
                    markets = resp.get("markets", [])
                    all_markets.extend(markets)
                    cursor = resp.get("cursor")
                    if not cursor or not markets:
                        break
        else:
            cursor = None
            while True:
                resp = self.client.get_markets(status="open", cursor=cursor)
                markets = resp.get("markets", [])
                all_markets.extend(markets)
                cursor = resp.get("cursor")
                if not cursor or not markets:
                    break

        logger.info("Fetched %d open markets", len(all_markets))
        return all_markets

    def _scan_specific_tickers(self, tickers: list[str]) -> list[MarketInfo]:
        """Build MarketInfo for a list of specific tickers."""
        results = []
        for ticker in tickers:
            try:
                market_resp = self.client.get_market(ticker)
                market = market_resp.get("market", market_resp)
                book = self.client.get_orderbook(ticker)
                info = self._build_market_info(market, book)
                if info:
                    info.score = self._compute_score(info)
                    results.append(info)
            except Exception as e:
                logger.warning("Failed to fetch ticker %s: %s", ticker, e)
        return sorted(results, key=lambda m: m.score, reverse=True)

    def _filter_markets(self, markets: list[dict]) -> list[MarketInfo]:
        """Filter markets by volume, open interest, and time to expiry."""
        viable = []
        now = datetime.now(timezone.utc)

        for market in markets:
            volume_24h = market.get("volume_24h", 0) or 0
            open_interest = market.get("open_interest", 0) or 0

            if volume_24h < self.config.min_volume_24h:
                continue
            if open_interest < self.config.min_open_interest:
                continue

            # Check time to expiry
            expiry_str = market.get("close_time") or market.get("expiration_time")
            if expiry_str:
                try:
                    expiry = datetime.fromisoformat(
                        expiry_str.replace("Z", "+00:00")
                    )
                    tte = (expiry - now).total_seconds()
                    if tte < self.config.min_time_to_expiry_sec:
                        continue
                except (ValueError, TypeError):
                    pass

            # Fetch orderbook for spread info
            try:
                book = self.client.get_orderbook(market["ticker"])
                info = self._build_market_info(market, book)
                if info:
                    viable.append(info)
            except Exception as e:
                logger.debug("Skipping %s: %s", market.get("ticker"), e)

        logger.info("Found %d viable markets", len(viable))
        return viable

    def _build_market_info(self, market: dict, book: dict) -> Optional[MarketInfo]:
        """Build a MarketInfo from market data and orderbook."""
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        volume_24h = market.get("volume_24h", 0) or 0
        open_interest = market.get("open_interest", 0) or 0

        now = datetime.now(timezone.utc)
        expiry_str = market.get("close_time") or market.get("expiration_time")
        tte = float("inf")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                tte = (expiry - now).total_seconds()
            except (ValueError, TypeError):
                pass

        # Parse orderbook
        # Kalshi orderbook returns yes bids and no bids
        # Best YES bid = highest price someone will pay for YES
        # Best YES ask = 100 - best NO bid (since a NO bid at X means willing to sell YES at 100-X)
        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        best_yes_bid = None
        best_yes_ask = None

        if yes_bids:
            # yes_bids is a list of [price, quantity] pairs
            best_yes_bid = max(lvl[0] for lvl in yes_bids)
        if no_bids:
            best_no_bid = max(lvl[0] for lvl in no_bids)
            best_yes_ask = 100 - best_no_bid

        spread = None
        if best_yes_bid is not None and best_yes_ask is not None:
            spread = best_yes_ask - best_yes_bid

        has_maker_fees = bool(market.get("maker_fee_bps", 0))

        return MarketInfo(
            ticker=ticker,
            title=title,
            volume_24h=volume_24h,
            open_interest=open_interest,
            yes_bid=best_yes_bid,
            yes_ask=best_yes_ask,
            spread=spread,
            time_to_expiry_sec=tte,
            has_maker_fees=has_maker_fees,
        )

    def _compute_score(self, info: MarketInfo) -> float:
        """Compute a ranking score. Higher is better for market making."""
        score = 0.0

        # Volume is the primary indicator of activity
        score += min(info.volume_24h, 5000) / 100  # cap contribution at 50

        # Open interest shows sustained interest
        score += min(info.open_interest, 2000) / 100  # cap at 20

        # Wider spreads mean more profit opportunity
        if info.spread is not None and info.spread > 0:
            score += min(info.spread, 15) * 2  # cap at 30

        # Prefer no maker fees
        if not info.has_maker_fees:
            score += 10

        # Penalize very short time to expiry
        if info.time_to_expiry_sec < 7200:
            score -= 20

        return score

    def _rank_markets(self, markets: list[MarketInfo]) -> list[MarketInfo]:
        """Rank markets by composite score."""
        for m in markets:
            m.score = self._compute_score(m)
        return sorted(markets, key=lambda m: m.score, reverse=True)
