"""Pi Trader — New Listing Detector.

Monitors Binance for newly listed USDC pairs.
New listings = guaranteed initial pump (retail FOMO).
Strategy: enter within 2min of listing, exit after 15min with tight 3% stop.

Detection method: poll exchangeInfo every 120s, cross-check against known symbols.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from binance_client import client
from config import config

logger = logging.getLogger(__name__)


@dataclass
class NewListing:
    symbol: str
    detected_at: float = field(default_factory=time.time)
    base_asset: str = ""


class ListingDetector:
    def __init__(self):
        self._known_symbols: set[str] = set()
        self._new_listings: list[NewListing] = []
        self._initialized = False
        self._stop_event = asyncio.Event()
        self._on_new_listing: list = []   # callbacks

    def on_listing(self, callback) -> None:
        """Register a callback to be called on new listing detection."""
        self._on_new_listing.append(callback)

    async def _load_symbols(self) -> set[str]:
        """Fetch all active USDC symbols from Binance."""
        try:
            symbols = await client.get_usdc_symbols()
            return set(symbols)
        except Exception as e:
            logger.warning(f"ListingDetector symbol fetch error: {e}")
            return set()

    async def start(self) -> None:
        logger.info("ListingDetector started")

        # Initial load — build baseline
        self._known_symbols = await self._load_symbols()
        self._initialized = True
        logger.info(f"ListingDetector: baseline {len(self._known_symbols)} USDC symbols")

        while not self._stop_event.is_set():
            await asyncio.sleep(config.listing_scan_interval)
            if self._stop_event.is_set():
                break
            await self._check_new_listings()

    async def _check_new_listings(self) -> None:
        current = await self._load_symbols()
        if not current:
            return

        new = current - self._known_symbols
        if new:
            for symbol in new:
                logger.info(f"NEW LISTING DETECTED: {symbol}")
                listing = NewListing(
                    symbol=symbol,
                    base_asset=symbol.replace("USDC", ""),
                )
                self._new_listings.append(listing)
                # Fire callbacks
                for cb in self._on_new_listing:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            asyncio.create_task(cb(listing))
                        else:
                            cb(listing)
                    except Exception as e:
                        logger.error(f"ListingDetector callback error: {e}")

        # Update known (also handle delistings gracefully)
        self._known_symbols = current

    def stop(self) -> None:
        self._stop_event.set()

    def get_recent(self, max_age_seconds: int = 300) -> list[NewListing]:
        """Listings detected in last N seconds (default 5min)."""
        cutoff = time.time() - max_age_seconds
        return [l for l in self._new_listings if l.detected_at >= cutoff]

    def is_new_listing(self, symbol: str, max_age_seconds: int = 300) -> bool:
        return any(
            l.symbol == symbol and l.detected_at >= time.time() - max_age_seconds
            for l in self._new_listings
        )

    def format_report(self) -> str:
        recent = self.get_recent(3600)  # last hour
        if not recent:
            return "No new listings in the last hour."
        lines = ["🆕 <b>New Listings (last 1h)</b>"]
        for l in recent:
            age_min = int((time.time() - l.detected_at) / 60)
            lines.append(f"• <b>{l.symbol}</b> — {age_min}min ago")
        return "\n".join(lines)


# Singleton
listing_detector = ListingDetector()
