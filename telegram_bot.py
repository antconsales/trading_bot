"""Pi Trader — Telegram bot interface.

Commands:
  /status   — portfolio + open positions + today's PnL
  /trades   — last 10 trades
  /pump     — latest pump candidates
  /listings — recently detected new listings
  /stop     — pause trading
  /start    — resume trading
  /mode safe|aggr — switch trading mode
  /perf     — weekly performance summary
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
from binance_client import client
from config import config
from engine import engine
from intelligence import intelligence
from listing_detector import listing_detector
from pump_detector import pump_detector

logger = logging.getLogger(__name__)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    status = engine.status()
    positions = db.get_positions()
    tier = await intelligence.get_tier()

    pos_lines = []
    for pos in positions:
        try:
            price = await client.ticker_price(pos["symbol"])
            pnl = (price - pos["entry_price"]) * pos["qty"]
            pos_lines.append(
                f"  • {pos['symbol']} | Entry: {pos['entry_price']:.4f} | "
                f"Now: {price:.4f} | PnL: {pnl:+.2f} USDC"
            )
        except Exception:
            pos_lines.append(f"  • {pos['symbol']} | Entry: {pos['entry_price']:.4f}")

    try:
        balance = await client.get_balance("USDC")
    except Exception:
        balance = 0.0

    mode_str = "🟡 PAPER" if config.paper_mode else "🔴 LIVE"
    state_str = "⏸ PAUSED" if status["paused"] else "▶️ RUNNING"
    tier_str = "🖥 AMR5 (8b)" if tier == "amr5" else "🍓 Pi (0.8b)"

    text = (
        f"<b>Pi Trader Status</b>\n"
        f"{mode_str} | {state_str} | {tier_str}\n\n"
        f"💰 USDC Balance: {balance:.2f}\n"
        f"📈 Today PnL: {status['today_pnl']:+.2f} USDC\n"
        f"📊 Open positions: {status['open_positions']}/{config.max_positions}\n"
        f"Mode: {status['mode']}\n"
    )
    if pos_lines:
        text += "\n<b>Positions:</b>\n" + "\n".join(pos_lines)
    else:
        text += "\nNo open positions."

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    trades = db.get_trades(limit=10)
    if not trades:
        await update.message.reply_text("No trades yet.")
        return

    lines = ["<b>Last 10 Trades</b>"]
    for t in trades:
        emoji = "✅" if t["pnl"] >= 0 else "❌"
        lines.append(
            f"{emoji} {t['action'].upper()} {t['symbol']} "
            f"@ {t['price']:.4f} | PnL: {t['pnl']:+.2f} | {t['ts'][:16]}"
        )

    total_pnl = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["action"] in ("sell", "partial_sell") and t["pnl"] > 0)
    sells = sum(1 for t in trades if t["action"] in ("sell", "partial_sell"))
    wr = wins / sells * 100 if sells else 0

    lines.append(f"\nTotal PnL: {total_pnl:+.2f} USDC | Win rate: {wr:.0f}%")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_pump(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    report = pump_detector.format_report(top=8)
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_listings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    report = listing_detector.format_report()
    await update.message.reply_text(report, parse_mode="HTML")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    engine.pause()
    await update.message.reply_text("⏸ Trading paused. Use /start to resume.")


async def cmd_start_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    engine.resume()
    await update.message.reply_text("▶️ Trading resumed.")


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args
    if not args or args[0] not in ("safe", "aggr", "full"):
        await update.message.reply_text("Usage: /mode safe|aggr|full")
        return
    mode_map = {"safe": "safe_only", "aggr": "full", "full": "full"}
    engine.set_mode(mode_map[args[0]])
    await update.message.reply_text(f"Mode set to: {mode_map[args[0]]}")


async def cmd_perf(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    perf = db.get_performance(days=7)
    if not perf:
        await update.message.reply_text("No performance data yet.")
        return

    lines = ["<b>Weekly Performance</b>"]
    total = 0.0
    for p in perf[:7]:
        emoji = "📈" if p["total_pnl"] >= 0 else "📉"
        lines.append(
            f"{emoji} {p['date']} | PnL: {p['total_pnl']:+.2f} | "
            f"Win: {p['win_rate']:.0f}% | Trades: {p['trades_count']}"
        )
        total += p["total_pnl"]

    lines.append(f"\n<b>7-day total: {total:+.2f} USDC</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def build_app() -> Application | None:
    if not config.telegram_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")
        return None

    app = Application.builder().token(config.telegram_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("pump", cmd_pump))
    app.add_handler(CommandHandler("listings", cmd_listings))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("start", cmd_start_bot))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("perf", cmd_perf))
    return app


async def run_bot() -> None:
    """Run bot in polling mode (for standalone use in asyncio)."""
    app = build_app()
    if not app:
        return
    logger.info("Telegram bot started (polling)")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
