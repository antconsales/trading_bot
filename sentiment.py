"""Pi Trader — Sentiment layer.

Sources:
- Fear & Greed Index (alternative.me, free, no auth)
- CoinGecko trending (free, no auth)
- Reddit mention velocity (subreddit JSON API, no auth)

All cached to avoid hammering free APIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from config import config

logger = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"
_REDDIT_JSON = "https://www.reddit.com/r/{sub}/search.json?q={sym}&sort=new&limit=25&restrict_sr=1"

# Subreddits to scan for velocity (broader coverage)
_REDDIT_SUBS = ["CryptoCurrency", "CryptoMoonShots", "altcoin", "SatoshiStreetBets"]

# Previous mention counts per symbol for growth-rate calculation
# Structure: {symbol: (timestamp, count)}
_prev_counts: dict[str, tuple[float, float]] = {}


@dataclass
class SentimentData:
    fear_greed: int = 50          # 0-100
    fear_greed_label: str = "Neutral"
    trending_coins: list[str] = field(default_factory=list)   # top CoinGecko trending
    multiplier: float = 1.0       # applied to signal confidence


_cache: dict[str, tuple[float, any]] = {}   # key → (timestamp, value)


def _cached(key: str, ttl: int = 900) -> any:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    return None


def _store(key: str, value: any) -> None:
    _cache[key] = (time.time(), value)


async def _fetch_json(url: str, timeout: int = 8) -> dict | list | None:
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"User-Agent": "Mozilla/5.0 (compatible; PiTrader/1.0)"},
            ) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
    except Exception as e:
        logger.debug(f"fetch_json {url}: {e}")
    return None


async def get_fear_greed() -> tuple[int, str]:
    """Returns (value 0-100, label). Cached 15min."""
    cached = _cached("fng", config.sentiment_cache_ttl)
    if cached is not None:
        return cached
    data = await _fetch_json(_FNG_URL)
    if data and isinstance(data, dict):
        entry = data.get("data", [{}])[0]
        val = int(entry.get("value", 50))
        label = entry.get("value_classification", "Neutral")
        result = (val, label)
        _store("fng", result)
        logger.debug(f"Fear&Greed: {val} ({label})")
        return result
    return (50, "Neutral")


async def get_trending_coins() -> list[str]:
    """Top 7 CoinGecko trending coins (symbols). Cached 30min."""
    cached = _cached("trending", 1800)
    if cached is not None:
        return cached
    data = await _fetch_json(_COINGECKO_TRENDING)
    if not data:
        return []
    coins: list[str] = []
    for item in data.get("coins", []):
        sym = item.get("item", {}).get("symbol", "")
        if sym:
            coins.append(sym.upper())
    _store("trending", coins)
    logger.debug(f"CoinGecko trending: {coins}")
    return coins


async def _count_reddit_posts(base_sym: str, hours: int = 1) -> float:
    """Count posts mentioning base_sym across all subreddits in last `hours` hours."""
    cutoff = time.time() - hours * 3600
    total  = 0.0

    async def _fetch_sub(sub: str) -> int:
        url  = _REDDIT_JSON.format(sub=sub, sym=base_sym)
        data = await _fetch_json(url)
        if not data:
            return 0
        posts = data.get("data", {}).get("children", [])
        return sum(
            1 for p in posts
            if p.get("data", {}).get("created_utc", 0) >= cutoff
        )

    counts = await asyncio.gather(*[_fetch_sub(s) for s in _REDDIT_SUBS])
    total  = float(sum(counts))
    return total


async def get_reddit_velocity(symbol: str) -> float:
    """
    Reddit mention GROWTH RATE for symbol.

    Returns posts-per-hour now vs posts-per-hour in previous window.
    growth_rate > 1.0  → increasing mentions (bullish signal)
    growth_rate = 1.0  → stable
    growth_rate < 1.0  → declining

    Cached 5min per symbol.
    """
    key = f"reddit_{symbol}"
    cached = _cached(key, 300)
    if cached is not None:
        return cached

    base_sym = symbol.replace("USDC", "").replace("USDT", "").replace("BTC", "")
    if not base_sym:
        return 0.0

    now       = time.time()
    current   = await _count_reddit_posts(base_sym, hours=1)

    # Calculate growth rate vs previous measurement
    growth_rate = 1.0
    prev = _prev_counts.get(symbol)
    if prev is not None:
        prev_ts, prev_count = prev
        elapsed_hours = (now - prev_ts) / 3600
        # Only compare if prev measurement is 5–90 min old (meaningful window)
        if 0.08 < elapsed_hours < 1.5 and prev_count > 0:
            # Annualise to per-hour rate
            growth_rate = current / max(prev_count, 1.0)
        elif prev_count == 0 and current > 0:
            growth_rate = 2.0   # sudden appearance = strong growth

    # Store current as new baseline
    _prev_counts[symbol] = (now, current)

    result = growth_rate
    _store(key, result)
    logger.debug(
        f"Reddit {base_sym}: {current:.0f} posts/h "
        f"(growth_rate={growth_rate:.2f}x, subs={_REDDIT_SUBS})"
    )
    return result


def _calc_multiplier(fng: int) -> float:
    """Convert Fear & Greed index to signal confidence multiplier."""
    if fng < 20:
        return 0.5   # extreme fear — very cautious
    elif fng < 45:
        return 0.8   # fear
    elif fng < 55:
        return 1.0   # neutral
    elif fng < 75:
        return 1.1   # greed — momentum helps
    else:
        return 0.7   # extreme greed — bubble risk


async def get_sentiment(symbol: str | None = None) -> SentimentData:
    """Full sentiment snapshot. If symbol provided, also checks Reddit velocity."""
    fng_val, fng_label = await get_fear_greed()
    trending = await get_trending_coins()

    mult = _calc_multiplier(fng_val)

    # Trending coin boost
    if symbol:
        base = symbol.replace("USDC", "").replace("USDT", "")
        if base in trending:
            mult = min(mult * 1.15, 1.3)
            logger.debug(f"{base} is CoinGecko trending — boosting multiplier to {mult:.2f}")

        # Reddit velocity growth-rate boost
        growth = await get_reddit_velocity(symbol)
        if growth >= 2.0:          # mentions doubling → strong boost
            mult = min(mult * 1.15, 1.3)
        elif growth >= 1.5:        # 50% increase → mild boost
            mult = min(mult * 1.08, 1.3)
        elif growth < 0.5:         # mentions dropping → slight dampener
            mult *= 0.95

    return SentimentData(
        fear_greed=fng_val,
        fear_greed_label=fng_label,
        trending_coins=trending,
        multiplier=mult,
    )
