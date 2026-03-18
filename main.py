"""Pi Trader — Entrypoint.

Starts all background services concurrently:
  - Trading engine (60s cycle)
  - Pump detector (60s scan, 200+ pairs)
  - Listing detector (120s poll)
  - Telegram bot (polling)
  - Autotuner (weekly)

Handles SIGTERM/SIGINT for clean shutdown.
"""

import asyncio
import logging
import os
import signal
import sys

# Configure logging before imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pi_trader.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("pi_trader")

# Now import modules (they use logging)
import db
from autotuner import start_autotuner_loop
from binance_client import client
from config import config
from engine import engine
from listing_detector import listing_detector
from notify import notify
from pump_detector import pump_detector
from telegram_bot import run_bot


def _restore_config_from_db() -> None:
    """Load autotuner-saved thresholds from DB on startup."""
    rsi = db.config_get("rsi_oversold")
    if rsi is not None:
        config.rsi_oversold = float(rsi)
        logger.info(f"Restored RSI oversold: {rsi}")

    vol = db.config_get("vol_ratio")
    if vol is not None:
        config.volume_ratio_threshold = float(vol)
        logger.info(f"Restored vol ratio: {vol}")


async def main() -> None:
    logger.info("Pi Trader starting...")

    # Validate config
    warnings = config.validate()
    for w in warnings:
        logger.warning(f"CONFIG: {w}")

    # Init DB
    db.init(config.db_path)
    _restore_config_from_db()

    # Init Binance client
    await client.start()
    logger.info("Binance client ready")

    # Connectivity check
    if await client.ping():
        logger.info("Binance API: OK")
    else:
        logger.error("Binance API unreachable — check internet connection")

    # Pre-warm local LLM — fire-and-forget so it doesn't block ZeroClaw responses
    async def _prewarm():
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as _s:
                async with _s.post(
                    f"{config.local_ollama_url}/api/generate",
                    json={"model": config.local_model, "prompt": "ping", "stream": False,
                          "options": {"num_predict": 1}},
                    timeout=_aiohttp.ClientTimeout(total=65),
                ) as _r:
                    if _r.status == 200:
                        logger.info("Local LLM warm and ready")
                    else:
                        logger.warning(f"LLM pre-warm status: {_r.status}")
        except Exception as _e:
            logger.warning(f"LLM pre-warm failed (will retry on first trade): {_e}")

    asyncio.create_task(_prewarm())
    logger.info("LLM pre-warm started in background (non-blocking)")

    # Send startup notification
    await notify.send_startup()

    # Create all background tasks
    tasks = []

    # Core engine
    tasks.append(asyncio.create_task(engine.start(), name="engine"))

    # Pump detector
    tasks.append(asyncio.create_task(pump_detector.start(), name="pump_detector"))

    # Listing detector
    tasks.append(asyncio.create_task(listing_detector.start(), name="listing_detector"))

    # Autotuner
    tasks.append(asyncio.create_task(start_autotuner_loop(), name="autotuner"))

    # Telegram bot — disabled if ZeroClaw or another agent is already polling this token
    # Set ENABLE_TELEGRAM_BOT=true in .env to enable command handler (conflicts with ZeroClaw)
    if os.environ.get("ENABLE_TELEGRAM_BOT", "false").lower() == "true":
        tasks.append(asyncio.create_task(run_bot(), name="telegram_bot"))
    else:
        logger.info("Telegram command bot disabled (ZeroClaw handles Telegram on this Pi)")

    # Monitor tasks for crashes
    stop_event = asyncio.Event()

    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Task '{task.get_name()}' crashed: {type(exc).__name__}: {exc}")
            notify.send_sync(f"🔴 Pi Trader crash: {task.get_name()} — {type(exc).__name__}: {exc}")

    for t in tasks:
        t.add_done_callback(_on_task_done)

    # Signal handlers
    loop = asyncio.get_running_loop()

    def _handle_shutdown(sig_name: str) -> None:
        logger.info(f"Received {sig_name} — shutting down...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_shutdown, sig.name)
        except NotImplementedError:
            # Windows
            pass

    logger.info("All services started — Pi Trader running")

    # Wait for shutdown signal
    await stop_event.wait()

    # Graceful shutdown
    logger.info("Shutting down Pi Trader...")

    engine.stop()
    pump_detector.stop()
    listing_detector.stop()

    for t in tasks:
        t.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    await client.stop()

    logger.info("Pi Trader stopped cleanly.")
    await notify.send("🤖 Pi Trader stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
