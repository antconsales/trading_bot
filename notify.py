"""Pi Trader — Telegram notifications.

Thin async wrapper. Gracefully handles missing config (no crash).
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from config import config

logger = logging.getLogger(__name__)

_TG_BASE  = "https://api.telegram.org/bot{token}"
_TG_URL   = _TG_BASE + "/sendMessage"
_TG_CMDS  = _TG_BASE + "/setMyCommands"

# Persistent keyboard shown at the bottom of the chat.
# Tapping a button sends the text as a message → ZeroClaw skill handles it.
_MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "/status"},   {"text": "/trades"},   {"text": "/positions"}],
        [{"text": "/pump"},     {"text": "/listings"},  {"text": "/perf"}],
        [{"text": "/stop"},     {"text": "/resume"},    {"text": "/full"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
    "input_field_placeholder": "Pi Trader command…",
}

_BOT_COMMANDS = [
    {"command": "status",    "description": "Portfolio, posizioni aperte, P&L oggi"},
    {"command": "trades",    "description": "Ultimi 10 trade"},
    {"command": "positions", "description": "Dettaglio posizioni aperte"},
    {"command": "perf",      "description": "Performance 7 giorni"},
    {"command": "pump",      "description": "Pump candidates rilevati"},
    {"command": "listings",  "description": "Nuovi listing Binance"},
    {"command": "stop",      "description": "Pausa il trading engine"},
    {"command": "resume",    "description": "Riprendi il trading"},
    {"command": "safe",      "description": "Modalità safe-only (BTC/ETH/SOL/XRP)"},
    {"command": "full",      "description": "Modalità full (tutti 8 pair + pumps)"},
    {"command": "config",    "description": "Mostra soglie RSI/BB/vol attuali"},
]


class Notifier:
    def __init__(self):
        self._token = config.telegram_token
        self._chat_id = config.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)

    async def set_commands(self) -> None:
        """Register bot commands with Telegram (shows in '/' menu)."""
        if not self._enabled:
            return
        try:
            url = _TG_CMDS.format(token=self._token)
            async with aiohttp.ClientSession() as sess:
                async with sess.post(
                    url,
                    json={"commands": _BOT_COMMANDS},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        logger.info("Telegram commands registered")
                    else:
                        body = await r.text()
                        logger.warning(f"setMyCommands failed {r.status}: {body[:100]}")
        except Exception as e:
            logger.warning(f"setMyCommands error: {e}")

    async def send(self, text: str, parse_mode: str = "HTML", keyboard: bool = False) -> bool:
        if not self._enabled:
            logger.debug(f"[notify disabled] {text[:80]}")
            return False
        try:
            url = _TG_URL.format(token=self._token)
            payload: dict = {
                "chat_id": self._chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if keyboard:
                payload["reply_markup"] = _MAIN_KEYBOARD
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
        mode = "🔴 LIVE" if not config.paper_mode else "🟡 PAPER"
        await self.set_commands()
        await self.send(
            f"🤖 <b>Pi Trader avviato</b> — {mode}\n"
            f"Coppie: {len(config.all_symbols)} ({len(config.safe_symbols)} safe + {len(config.aggr_symbols)} aggr)\n"
            f"Max posizioni: {config.max_positions} | Risk: {config.risk_per_trade:.0%}/trade\n"
            f"LLM: {config.local_model}\n\n"
            f"<i>Usa i bottoni qui sotto per controllare il bot 👇</i>",
            keyboard=True,
        )


# Singleton
notify = Notifier()
