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

    # Total portfolio value
    positions_value = sum(
        (await client.ticker_price(pos["symbol"])) * pos["qty"]
        for pos in positions
    ) if positions else 0.0
    total = balance + positions_value

    mode_str = "🟡 PAPER" if config.paper_mode else "🔴 LIVE"
    state_str = "⏸ PAUSED" if status["paused"] else "▶️ RUNNING"
    tier_str = "🖥 AMR5 (8b)" if tier == "amr5" else "🍓 Pi (0.8b)"

    text = (
        f"<b>Pi Trader Status</b>\n"
        f"{mode_str} | {state_str} | {tier_str}\n\n"
        f"💼 <b>Portfolio totale: {total:.2f} USDC</b>\n"
        f"   ├ USDC libero: {balance:.2f}\n"
        f"   └ In posizioni: {positions_value:.2f}\n"
        f"📈 Today PnL: {status['today_pnl']:+.2f} USDC\n"
        f"📊 Posizioni: {status['open_positions']}/{config.max_positions} | Mode: {status['mode']}\n"
    )
    if pos_lines:
        text += "\n<b>Positions:</b>\n" + "\n".join(pos_lines)
    else:
        text += "\nNessuna posizione aperta."

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    positions = db.get_positions()
    if not positions:
        await update.message.reply_text("Nessuna posizione aperta.")
        return
    lines = ["<b>Posizioni aperte</b>"]
    for pos in positions:
        side = pos.get("side", "long")
        side_icon = "\U0001f7e2" if side == "long" else "\U0001f534"
        try:
            price = await client.ticker_price(pos["symbol"])
            if side == "short":
                pnl = (pos["entry_price"] - price) * pos["qty"]
                pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
            else:
                pnl = (price - pos["entry_price"]) * pos["qty"]
                pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            lines.append(
                f"\n{side_icon} <b>{pos['symbol']}</b> [{pos.get('pool','?')}] {side.upper()}\n"
                f"  Entry: {pos['entry_price']:.4f} | Now: {price:.4f} ({pct:+.1f}%)\n"
                f"  PnL: {pnl:+.2f} USDC | Qty: {pos['qty']:.4f}\n"
                f"  Stop: {pos['stop_loss']:.4f} | TP: {pos['take_profit']:.4f}"
            )
        except Exception:
            lines.append(f"\n{side_icon} <b>{pos['symbol']}</b> [{side.upper()}] | Entry: {pos['entry_price']:.4f}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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




async def cmd_shorts(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from config import config as cfg
    if not cfg.enable_shorts:
        await update.message.reply_text(
            "Short selling disabilitato.\nAbilita con ENABLE_SHORTS=true nel .env"
        )
        return

    positions = db.get_positions()
    shorts = [p for p in positions if p.get("side") == "short"]

    if not shorts:
        await update.message.reply_text("Nessuna posizione short aperta.")
        return

    lines = ["<b>\U0001f534 Short Positions</b>"]
    for pos in shorts:
        try:
            price = await client.ticker_price(pos["symbol"])
            pnl = (pos["entry_price"] - price) * pos["qty"] * cfg.futures_leverage
            pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
            lines.append(
                f"\n<b>{pos['symbol']}</b> {cfg.futures_leverage}x\n"
                f"  Entry: {pos['entry_price']:.4f} | Now: {price:.4f} ({pct:+.1f}%)\n"
                f"  PnL: {pnl:+.2f} USDC | Qty: {pos['qty']:.4f}\n"
                f"  Stop: {pos['stop_loss']:.4f} | TP: {pos['take_profit']:.4f}\n"
                f"  Usa /closeshort {pos['symbol'].replace('USDC','').replace('USDT','')} per chiudere"
            )
        except Exception:
            lines.append(f"\n<b>{pos['symbol']}</b> [SHORT] | Entry: {pos['entry_price']:.4f}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_closeshort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually close a short position: /closeshort BTC"""
    from config import config as cfg
    from binance_client import futures_client
    from engine import engine

    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /closeshort <symbol>  es: /closeshort BTC")
        return

    base = args[0].upper()
    symbol = base + "USDC" if not base.endswith(("USDC", "USDT")) else base
    pos = db.get_position(symbol)
    if not pos or pos.get("side") != "short":
        await update.message.reply_text(f"Nessuna short aperta su {symbol}")
        return

    try:
        price = await client.ticker_price(symbol)
        await engine._close_short(symbol, pos["qty"], price, "manual_telegram")
        await update.message.reply_text(f"\u2705 Short {symbol} chiusa @ {price:.4f}")
    except Exception as e:
        await update.message.reply_text(f"\u274c Errore chiusura short: {e}")

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
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("shorts", cmd_shorts))
    app.add_handler(CommandHandler("closeshort", cmd_closeshort))
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


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        usdc = await client.get_balance("USDC")
    except Exception:
        usdc = 0.0
    safe = ", ".join(s.replace("USDC", "") for s in config.safe_symbols)
    aggr = ", ".join(s.replace("USDC", "") for s in config.aggr_symbols)
    text = (
        f"<b>Pi Trader Config</b>\n\n"
        f"<b>Capitale:</b>\n"
        f"  USDC libero: {usdc:.2f}\n"
        f"  ⚠️ Il bot usa solo USDC — converti manualmente le altre coin\n\n"
        f"<b>Soglie segnali:</b>\n"
        f"  RSI oversold: {config.rsi_oversold} | overbought: {config.rsi_overbought}\n"
        f"  Vol ratio min: {config.volume_ratio_threshold}x\n"
        f"  BB squeeze: {config.bb_squeeze_threshold}\n\n"
        f"<b>Risk management:</b>\n"
        f"  Risk/trade: {config.risk_per_trade:.0%} | Max pos: {config.max_positions}\n"
        f"  Daily loss limit: {config.daily_loss_limit:.0%} | Hold max: {config.max_hold_hours}h\n"
        f"  Stop: {config.stop_loss_atr}x ATR | TP: {config.take_profit_atr}x ATR\n\n"
        f"<b>Pool:</b>\n"
        f"  Safe 70%: {safe}\n"
        f"  Aggr 30%: {aggr}"
    )
    await update.message.reply_text(text, parse_mode="HTML")
