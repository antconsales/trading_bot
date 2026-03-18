"""Pi Trader — Binance REST + WebSocket client.

Uses aiohttp only (no binance-connector SDK to save RAM).
Handles signature, rate-limit headers, reconnecting WebSocket streams.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlencode

import aiohttp

from config import config

logger = logging.getLogger(__name__)

BASE_REST = "https://api.binance.com"
BASE_WS = "wss://stream.binance.com:9443/ws"


class BinanceError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        super().__init__(f"Binance error {code}: {msg}")


class BinanceClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._recv_window = 5000

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"X-MBX-APIKEY": config.binance_api_key},
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self._recv_window
        query = urlencode(params)
        sig = hmac.new(
            config.binance_api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = sig
        return params

    async def _get(self, path: str, params: dict | None = None, signed: bool = False) -> Any:
        assert self._session
        if signed:
            params = self._sign(params or {})
        async with self._session.get(f"{BASE_REST}{path}", params=params) as r:
            data = await r.json()
            if isinstance(data, dict) and "code" in data and data["code"] < 0:
                raise BinanceError(data["code"], data.get("msg", ""))
            return data

    async def _post(self, path: str, params: dict | None = None) -> Any:
        assert self._session
        params = self._sign(params or {})
        async with self._session.post(f"{BASE_REST}{path}", params=params) as r:
            data = await r.json()
            if isinstance(data, dict) and "code" in data and data["code"] < 0:
                raise BinanceError(data["code"], data.get("msg", ""))
            return data

    async def _delete(self, path: str, params: dict | None = None) -> Any:
        assert self._session
        params = self._sign(params or {})
        async with self._session.delete(f"{BASE_REST}{path}", params=params) as r:
            data = await r.json()
            if isinstance(data, dict) and "code" in data and data["code"] < 0:
                raise BinanceError(data["code"], data.get("msg", ""))
            return data

    # ── Market data ───────────────────────────────────────────────────────────

    async def ping(self) -> bool:
        try:
            await self._get("/api/v3/ping")
            return True
        except Exception:
            return False

    async def server_time(self) -> int:
        data = await self._get("/api/v3/time")
        return data["serverTime"]

    async def exchange_info(self) -> dict:
        return await self._get("/api/v3/exchangeInfo")

    async def get_usdc_symbols(self) -> list[str]:
        """All active XXXUSDC trading pairs."""
        info = await self.exchange_info()
        return [
            s["symbol"]
            for s in info.get("symbols", [])
            if s["quoteAsset"] == "USDC" and s["status"] == "TRADING"
        ]

    async def ticker_24h_all(self) -> list[dict]:
        """Bulk 24h ticker stats for ALL symbols. Single API call."""
        return await self._get("/api/v3/ticker/24hr")

    async def ticker_24h(self, symbol: str) -> dict:
        return await self._get("/api/v3/ticker/24hr", {"symbol": symbol})

    async def ticker_price(self, symbol: str) -> float:
        data = await self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """OHLCV candles. interval: 1m, 5m, 15m, 1h, 4h, 1d."""
        raw = await self._get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        return [
            {
                "ts": r[0],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
            for r in raw
        ]

    async def order_book(self, symbol: str, limit: int = 20) -> dict:
        """Returns {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}."""
        raw = await self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})
        return {
            "bids": [[float(p), float(q)] for p, q in raw.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in raw.get("asks", [])],
        }

    # ── Account ───────────────────────────────────────────────────────────────

    async def account(self) -> dict:
        return await self._get("/api/v3/account", signed=True)

    async def get_balance(self, asset: str = "USDC") -> float:
        data = await self.account()
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        data = await self.account()
        return {
            b["asset"]: float(b["free"])
            for b in data.get("balances", [])
            if float(b["free"]) > 0 or float(b["locked"]) > 0
        }

    # ── Orders ────────────────────────────────────────────────────────────────

    async def market_buy(self, symbol: str, quote_qty: float) -> dict:
        """Buy `quote_qty` USDC worth of symbol."""
        if config.paper_mode:
            price = await self.ticker_price(symbol)
            qty = quote_qty / price
            logger.info(f"[PAPER] BUY {symbol} qty={qty:.6f} price={price:.4f}")
            return {"symbol": symbol, "side": "BUY", "price": price, "qty": qty, "paper": True}
        return await self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.2f}",
        })

    async def market_sell(self, symbol: str, qty: float) -> dict:
        """Sell exact `qty` of base asset."""
        if config.paper_mode:
            price = await self.ticker_price(symbol)
            logger.info(f"[PAPER] SELL {symbol} qty={qty:.6f} price={price:.4f}")
            return {"symbol": symbol, "side": "SELL", "price": price, "qty": qty, "paper": True}
        return await self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.6f}",
        })

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict = {}
        if symbol:
            params["symbol"] = symbol
        return await self._get("/api/v3/openOrders", params, signed=True)

    # ── WebSocket streams ─────────────────────────────────────────────────────

    async def kline_stream(
        self,
        symbol: str,
        interval: str,
        callback: Callable[[dict], None],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Subscribe to kline stream. Calls callback on each closed candle."""
        stream = f"{symbol.lower()}@kline_{interval}"
        url = f"{BASE_WS}/{stream}"
        while not (stop_event and stop_event.is_set()):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(url, heartbeat=20) as ws:
                        logger.info(f"WS connected: {stream}")
                        async for msg in ws:
                            if stop_event and stop_event.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                k = data.get("k", {})
                                if k.get("x"):  # candle closed
                                    callback({
                                        "symbol": symbol,
                                        "interval": interval,
                                        "ts": k["t"],
                                        "open": float(k["o"]),
                                        "high": float(k["h"]),
                                        "low": float(k["l"]),
                                        "close": float(k["c"]),
                                        "volume": float(k["v"]),
                                    })
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
            except Exception as e:
                logger.warning(f"WS {stream} error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def book_ticker_stream(
        self,
        symbols: list[str],
        callback: Callable[[dict], None],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Subscribe to best bid/ask stream for multiple symbols."""
        streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
        url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        while not (stop_event and stop_event.is_set()):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(url, heartbeat=20) as ws:
                        logger.info(f"WS bookTicker connected: {len(symbols)} symbols")
                        async for msg in ws:
                            if stop_event and stop_event.is_set():
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                callback(data.get("data", data))
            except Exception as e:
                logger.warning(f"WS bookTicker error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)


# Singleton
client = BinanceClient()
