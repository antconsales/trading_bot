"""Pi Trader — Telegram notifications.

Thin async wrapper. Gracefully handles missing config (no crash).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from config import config

logger = logging.getLogger(__name__)

_TG_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self):
        self._token = config.telegram_token
        self._chat_id = config.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self._enabled:
            logger.debug(f"[notify disabled] {text[:80]}")
            return False
        try:
            url = _TG_URL.format(token=self._token)
            payload = {
                "chat_id": self._chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        logger.warning(f"Telegram send failed {r.status}: {body[:200]}")
                        return False
                    return True
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")
            return False

    def send_sync(self, text: str) -> bool:
        """Fire-and-forget in current event loop."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self.send(text))
                return True
        except Exception:
            pass
        return False

    async def send_crash(self, component: str, error: str) -> None:
        await self.send(
            f"🔴 <b>Pi Trader CRASH</b>\n"
            f"Component: {component}\n"
            f"Error: {error[:200]}"
        )

    async def send_startup(self) -> None:
        mode = "PAPER" if config.paper_mode else "LIVE"
        await self.send(
            f"🤖 <b>Pi Trader started</b>\n"
            f"Mode: {mode}\n"
            f"Max positions: {config.max_positions}\n"
            f"Risk/trade: {config.risk_per_trade:.0%}\n"
            f"LLM: {config.local_model}"
        )


# Singleton
notify = Notifier()
