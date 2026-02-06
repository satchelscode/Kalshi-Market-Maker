"""Market scanner: identifies viable markets for market making."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from api_client import KalshiClient
from config import ScannerConfig

logger = logging.getLogger(__name__)

# Max pages to fetch per series (100 markets per page)
MAX_SCAN_PAGES = 2

# Moneyline series have separate tickers per team in the same game.
# e.g., KXNBAGAME-25FEB04-TOR-v-DET-TOR and KXNBAGAME-25FEB04-TOR-v-DET-DET
# We must only quote ONE ticker per game, otherwise we accumulate the same
# directional bet through both tickers (YES TeamA = NO TeamB).
MONEYLINE_SERIES = {"KXNCAAMBGAME", "KXNCAAWBGAME", "KXNBAGAME"}


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
        candidates = self._prefilter(markets)
        viable = self._enrich_with_orderbooks(candidates)
        ranked = self._rank_markets(viable)
        return ranked[: self.config.max_markets]

    def _fetch_markets(self) -> list[dict]:
        """Fetch open markets with a page cap to avoid OOM."""
        all_markets = []
        logger.info(
            "Scanner config: min_vol=%d, min_oi=%d, min_tte=%ds",
            self.config.min_volume_24h,
            self.config.min_open_interest,
            self.config.min_time_to_expiry_sec,
        )

        if self.config.target_series:
            for series in self.config.target_series:
                cursor = None
                pages = 0
                while pages < MAX_SCAN_PAGES:
                    resp = self.client.get_markets(
                        status="open", series_ticker=series, cursor=cursor
                    )
                    markets = resp.get("markets", [])
                    all_markets.extend(markets)
                    cursor = resp.get("cursor")
                    pages += 1
                    if not cursor or not markets:
                        break
        else:
            cursor = None
            pages = 0
            while pages < MAX_SCAN_PAGES:
                resp = self.client.get_markets(status="open", cursor=cursor)
                markets = resp.get("markets", [])
                all_markets.extend(markets)
                cursor = resp.get("cursor")
                pages += 1
                if not cursor or not markets:
                    break

        logger.info("Fetched %d open markets (%d pages)", len(all_markets), pages)
        # Log a sample of what we got
        if all_markets:
            sample = all_markets[:3]
            for m in sample:
                logger.info(
                    "Sample market: %s vol=%s oi=%s close=%s",
                    m.get("ticker", "?"),
                    m.get("volume_24h", "?"),
                    m.get("open_interest", "?"),
                    m.get("close_time", m.get("expiration_time", "?")),
                )
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

    def _prefilter(self, markets: list[dict]) -> list[dict]:
        """Filter by volume/OI/expiry WITHOUT fetching orderbooks.

        Sort by volume descending and take only the top candidates
        to avoid excessive API calls.
        """
        now = datetime.now(timezone.utc)
        candidates = []

        for market in markets:
            volume_24h = market.get("volume_24h", 0) or 0
            open_interest = market.get("open_interest", 0) or 0

            if volume_24h < self.config.min_volume_24h:
                continue
            if open_interest < self.config.min_open_interest:
                continue

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

            candidates.append(market)

        # Sort by volume and only keep the top N for orderbook enrichment
        candidates.sort(key=lambda m: m.get("volume_24h", 0) or 0, reverse=True)
        top = candidates[: self.config.max_markets * 3]  # fetch 3x what we need
        logger.info(
            "Pre-filtered to %d candidates from %d markets",
            len(top), len(markets),
        )
        return top

    def _enrich_with_orderbooks(self, candidates: list[dict]) -> list[MarketInfo]:
        """Fetch orderbooks only for the top candidates."""
        viable = []
        for market in candidates:
            try:
                book = self.client.get_orderbook(market["ticker"])
                info = self._build_market_info(market, book)
                if info:
                    viable.append(info)
            except Exception as e:
                logger.debug("Skipping %s: %s", market.get("ticker"), e)
        logger.info("Enriched %d markets with orderbook data", len(viable))
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

        yes_bids = book.get("yes", [])
        no_bids = book.get("no", [])

        best_yes_bid = None
        best_yes_ask = None

        if yes_bids:
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

        score += min(info.volume_24h, 5000) / 100
        score += min(info.open_interest, 2000) / 100

        if info.spread is not None and info.spread > 0:
            score += min(info.spread, 15) * 2

        if not info.has_maker_fees:
            score += 10

        if info.time_to_expiry_sec < 7200:
            score -= 20

        return score

    def _rank_markets(self, markets: list[MarketInfo]) -> list[MarketInfo]:
        """Rank markets by composite score, deduplicating moneyline games."""
        for m in markets:
            m.score = self._compute_score(m)
        ranked = sorted(markets, key=lambda m: m.score, reverse=True)
        return self._dedup_moneyline(ranked)

    @staticmethod
    def _get_moneyline_game_key(ticker: str) -> Optional[str]:
        """Extract the game identifier from a moneyline ticker.

        e.g., 'KXNBAGAME-25FEB04-TOR-v-DET-TOR' -> 'KXNBAGAME-25FEB04-TOR-v-DET'
        For moneyline tickers, the last segment after the last '-' is the team.
        The rest is the game key shared by both team tickers.
        """
        for series in MONEYLINE_SERIES:
            if ticker.startswith(series):
                # Find the '-v-' pattern which separates the two teams
                v_idx = ticker.find("-v-")
                if v_idx == -1:
                    return None
                # After '-v-TEAM', there should be one more '-TEAM' at the end
                after_v = ticker[v_idx + 3:]  # e.g., 'DET-TOR' or 'DET-DET'
                dash_idx = after_v.find("-")
                if dash_idx == -1:
                    return None
                # Game key is everything except the final team segment
                game_key = ticker[:v_idx + 3 + dash_idx]
                return game_key
        return None

    @staticmethod
    def _dedup_moneyline(ranked: list["MarketInfo"]) -> list["MarketInfo"]:
        """Keep only the highest-scoring ticker per moneyline game.

        For moneyline markets, both team tickers in the same game represent
        the same underlying event. Quoting both leads to doubling up on
        directional risk (YES TeamA = NO TeamB).
        """
        seen_games: set[str] = set()
        result = []
        for m in ranked:
            game_key = MarketScanner._get_moneyline_game_key(m.ticker)
            if game_key is not None:
                if game_key in seen_games:
                    logger.info(
                        "Skipping %s (already quoting another ticker for game %s)",
                        m.ticker, game_key,
                    )
                    continue
                seen_games.add(game_key)
            result.append(m)
        return result
