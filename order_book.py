"""Pi Trader — Order book imbalance & whale detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from binance_client import client
from config import config

logger = logging.getLogger(__name__)


@dataclass
class OrderBookSignal:
    symbol: str
    imbalance: float          # 0.0–1.0 (bid / total)
    bid_volume: float
    ask_volume: float
    whale_bid: bool           # single big bid detected
    whale_ask: bool           # single big ask detected
    whale_bid_price: float    # price of biggest bid
    action: str               # "buy", "sell", "neutral"
    confidence: float         # 0.0–1.0


async def analyze(symbol: str, volume_24h: float | None = None) -> OrderBookSignal:
    """
    Fetch order book for symbol and compute imbalance + whale detection.

    volume_24h: 24h quote volume (USDC) — used to detect whale orders.
                If None, whale detection is skipped.
    """
    try:
        book = await client.order_book(symbol, limit=config.ob_depth)
    except Exception as e:
        logger.warning(f"OrderBook {symbol}: {e}")
        return OrderBookSignal(
            symbol=symbol, imbalance=0.5,
            bid_volume=0, ask_volume=0,
            whale_bid=False, whale_ask=False,
            whale_bid_price=0.0,
            action="neutral", confidence=0.0,
        )

    bids = book["bids"]   # [[price, qty], ...]
    asks = book["asks"]

    # Use top 10 levels for imbalance
    top = min(10, len(bids), len(asks))
    bid_vol = sum(p * q for p, q in bids[:top])
    ask_vol = sum(p * q for p, q in asks[:top])
    total = bid_vol + ask_vol
    imbalance = bid_vol / total if total > 0 else 0.5

    # Whale detection: single order > whale_order_pct of 24h vol
    whale_bid = False
    whale_ask = False
    whale_bid_price = 0.0
    biggest_bid = 0.0
    if volume_24h and volume_24h > 0:
        threshold = volume_24h * config.whale_order_pct
        for price, qty in bids:
            order_val = price * qty
            if order_val > threshold:
                whale_bid = True
                if order_val > biggest_bid:
                    biggest_bid = order_val
                    whale_bid_price = price
                break
        for price, qty in asks:
            order_val = price * qty
            if order_val > threshold:
                whale_ask = True
                break

    # Determine action
    if imbalance >= config.ob_imbalance_buy:
        action = "buy"
        confidence = (imbalance - 0.5) * 2   # 0→0.3 at 0.65, 1.0 at 1.0
    elif imbalance <= config.ob_imbalance_sell:
        action = "sell"
        confidence = (0.5 - imbalance) * 2
    else:
        action = "neutral"
        confidence = 0.0

    # Whale bid on buy side boosts confidence
    if whale_bid and action == "buy":
        confidence = min(confidence + 0.2, 1.0)
    if whale_ask and action == "sell":
        confidence = min(confidence + 0.2, 1.0)

    logger.debug(
        f"OB {symbol}: imbalance={imbalance:.2f} action={action} "
        f"whale_bid={whale_bid} whale_ask={whale_ask}"
    )

    return OrderBookSignal(
        symbol=symbol,
        imbalance=imbalance,
        bid_volume=bid_vol,
        ask_volume=ask_vol,
        whale_bid=whale_bid,
        whale_ask=whale_ask,
        whale_bid_price=whale_bid_price,
        action=action,
        confidence=confidence,
    )
