"""Pi Trader — Pump Detector.

Scans all USDC trading pairs every 60s for volume anomalies + price momentum.
Uses a single bulk API call for efficiency.

A "pump candidate" is a coin with:
  - Volume z-score > 3.0 (vs 7-day rolling window stored in memory)
  - Price change > 2% in last 15min (approximated via 24h stats)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from binance_client import client
from config import config
from indicators import zscore

logger = logging.getLogger(__name__)


@dataclass
class PumpCandidate:
    symbol: str
    price: float
    price_change_pct: float    # % change vs 24h ago (used as proxy for momentum)
    volume_24h: float
    volume_zscore: float
    score: float               # combined score, higher = more interesting
    detected_at: float = field(default_factory=time.time)


class PumpDetector:
    def __init__(self):
        # Rolling volume history: symbol → list of (timestamp, volume) tuples
        # We keep ~7 days at 1 sample/min = 10080 max, but 168 samples is plenty (every hour)
        self._vol_history: dict[str, list[float]] = {}
        self._max_history = 168     # 7 days × 24h
        self._last_candidates: list[PumpCandidate] = []
        self._running = False
        self._stop_event = asyncio.Event()

    async def _scan_once(self) -> list[PumpCandidate]:
        """Single scan: fetch all tickers, compute z-scores, return candidates."""
        try:
            all_tickers = await client.ticker_24h_all()
        except Exception as e:
            logger.warning(f"PumpDetector ticker fetch error: {e}")
            return []

        # Filter USDC pairs only
        usdc = [
            t for t in all_tickers
            if isinstance(t, dict) and t.get("symbol", "").endswith("USDC")
        ]

        candidates: list[PumpCandidate] = []

        for t in usdc:
            symbol = t["symbol"]
            try:
                vol_24h = float(t.get("quoteVolume", 0) or 0)
                price_change = float(t.get("priceChangePercent", 0) or 0)
                last_price = float(t.get("lastPrice", 0) or 0)
            except (ValueError, TypeError):
                continue

            if vol_24h < 10_000:  # Skip coins with < $10k 24h volume (noise)
                continue

            # Update rolling history
            hist = self._vol_history.setdefault(symbol, [])
            hist.append(vol_24h)
            if len(hist) > self._max_history:
                hist.pop(0)

            # Need at least 5 samples for z-score
            if len(hist) < 5:
                continue

            zs = zscore(hist)
            if zs is None:
                continue

            # Score = z-score × price_change_factor
            # We use 1h price change if available, else 24h change as proxy
            # Binance 24h change isn't ideal for 15min detection, but it's free
            price_factor = max(price_change / 5.0, 0.0)  # normalize: 5% change = factor 1.0
            score = zs * (1.0 + price_factor)

            if zs >= config.pump_volume_zscore and price_change >= config.pump_price_change_pct:
                candidates.append(PumpCandidate(
                    symbol=symbol,
                    price=last_price,
                    price_change_pct=price_change,
                    volume_24h=vol_24h,
                    volume_zscore=zs,
                    score=score,
                ))

        candidates.sort(key=lambda c: -c.score)
        logger.info(f"PumpDetector: scanned {len(usdc)} pairs, {len(candidates)} candidates")
        return candidates

    async def start(self) -> None:
        self._running = True
        logger.info("PumpDetector started")
        while not self._stop_event.is_set():
            start = time.time()
            self._last_candidates = await self._scan_once()
            if self._last_candidates:
                logger.info(
                    f"Pump candidates: {[c.symbol for c in self._last_candidates[:5]]}"
                )
            elapsed = time.time() - start
            sleep_time = max(0.0, config.pump_scan_interval - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.sleep(sleep_time)),
                    timeout=sleep_time + 1,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                break

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def get_candidates(self, min_score: float = 3.0) -> list[PumpCandidate]:
        """Return current pump candidates above threshold."""
        return [c for c in self._last_candidates if c.score >= min_score]

    def is_pump(self, symbol: str, min_score: float = 3.0) -> PumpCandidate | None:
        """Check if a specific symbol is currently flagged as pumping."""
        for c in self._last_candidates:
            if c.symbol == symbol and c.score >= min_score:
                return c
        return None

    def format_report(self, top: int = 5) -> str:
        candidates = self._last_candidates[:top]
        if not candidates:
            return "No pump candidates detected."
        lines = ["🚀 <b>Pump Candidates</b>"]
        for c in candidates:
            lines.append(
                f"• <b>{c.symbol}</b> | "
                f"Vol z={c.volume_zscore:.1f} | "
                f"Δ{c.price_change_pct:+.1f}% | "
                f"Score: {c.score:.1f}"
            )
        return "\n".join(lines)


# Singleton
pump_detector = PumpDetector()
