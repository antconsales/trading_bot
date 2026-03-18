"""Pi Trader — Core Trading Engine.

Main loop: 60s cycle for signal scanning.
WebSocket loop: real-time position monitoring (stop/TP triggers instantly).

Capital split:
  - 70% safe pool → BTC, ETH, SOL, XRP
  - 30% aggressive pool → SUI, NEAR, DOGE, PEPE + pumps/listings
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import db
import indicators as ind
import order_book
import sentiment as sent
from binance_client import client, BinanceError
from config import config
from intelligence import intelligence, LLMValidation
from listing_detector import listing_detector, NewListing
from notify import notify
from pump_detector import pump_detector, PumpCandidate

logger = logging.getLogger(__name__)

# ZeroClaw workspace IPC files
_ZEROCLAW_WORKSPACE = Path.home() / ".zeroclaw" / "workspace"
_STATUS_FILE = _ZEROCLAW_WORKSPACE / "pi_trader_status.json"
_CMD_FILE    = _ZEROCLAW_WORKSPACE / "pi_trader_cmd.json"


@dataclass
class TradeSignal:
    symbol: str
    source: str        # "pump", "listing", "standard", "ob_whale"
    score: float       # pre-LLM score
    price: float
    volume_24h: float
    rsi: float | None
    ema_trend: str
    bb_pct: float | None
    vol_ratio: float | None
    ob_imbalance: float
    whale_bid: bool
    fear_greed: int
    fear_greed_label: str
    pump_zscore: float
    pool: str          # "safe" or "aggressive"
    mtf_score: float = 0.0        # multi-timeframe confluence score
    mtf_direction: str = "neutral" # "long", "short", "neutral"
    mtf_agreement: int = 0         # 0-3 timeframes agreeing


class TradingEngine:
    def __init__(self):
        self._running = False
        self._paused = False
        self._stop_event = asyncio.Event()
        self._mode = "full"   # "full" | "safe_only"

        # Daily loss tracking
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""
        self._paused_until: float = 0.0     # unix timestamp

        # Consecutive loss tracker
        self._consecutive_losses: int = 0

    # ── State ─────────────────────────────────────────────────────────────────

    def pause(self, duration_sec: int = 0) -> None:
        self._paused = True
        if duration_sec:
            self._paused_until = time.time() + duration_sec
        logger.info(f"Engine paused (duration={duration_sec}s)")

    def resume(self) -> None:
        self._paused = False
        self._paused_until = 0.0
        logger.info("Engine resumed")

    def set_mode(self, mode: str) -> None:
        if mode in ("full", "safe_only"):
            self._mode = mode
            logger.info(f"Engine mode: {mode}")

    @property
    def is_paused(self) -> bool:
        if self._paused_until and time.time() >= self._paused_until:
            self._paused = False
            self._paused_until = 0.0
        return self._paused

    # ── Capital management ────────────────────────────────────────────────────

    async def _get_portfolio_value(self) -> float:
        """Total USDC balance + value of open positions."""
        try:
            usdc = await client.get_balance("USDC")
            positions = db.get_positions()
            position_value = 0.0
            for pos in positions:
                try:
                    price = await client.ticker_price(pos["symbol"])
                    position_value += pos["qty"] * price
                except Exception:
                    position_value += pos["qty"] * pos["entry_price"]
            return usdc + position_value
        except Exception as e:
            logger.warning(f"Portfolio value error: {e}")
            return 100.0  # fallback

    def _position_size(self, pool: str, portfolio_value: float) -> float:
        """USDC amount to allocate for this trade."""
        pool_fraction = config.safe_pool_ratio if pool == "safe" else (1 - config.safe_pool_ratio)
        pool_value = portfolio_value * pool_fraction
        return pool_value * config.risk_per_trade

    # ── Daily loss guard ──────────────────────────────────────────────────────

    def _check_daily_loss(self) -> bool:
        """Returns True if we can trade (daily loss limit not hit)."""
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._daily_pnl_date:
            self._daily_pnl = db.get_today_pnl()
            self._daily_pnl_date = today

        portfolio = 100.0  # approximate; we update it in the main loop
        if self._daily_pnl < -(portfolio * config.daily_loss_limit):
            logger.warning(f"Daily loss limit hit: {self._daily_pnl:.2f} USDC")
            self.pause(duration_sec=86400)
            notify.send_sync(f"⚠️ Daily loss limit hit ({self._daily_pnl:.2f} USDC). Pausing 24h.")
            return False
        return True

    # ── Position management ───────────────────────────────────────────────────

    async def _manage_positions(self) -> None:
        """Check each open position for stop/TP/trail/timeout."""
        positions = db.get_positions()
        for pos in positions:
            symbol = pos["symbol"]
            try:
                price = await client.ticker_price(symbol)
            except Exception as e:
                logger.warning(f"Price fetch {symbol}: {e}")
                continue

            entry = pos["entry_price"]
            qty = pos["qty"]
            stop = pos["stop_loss"]
            tp = pos["take_profit"]
            trail = pos["trail_price"]
            highest = pos["highest_price"]
            partial_sold = bool(pos["partial_sold"])
            is_listing = bool(pos.get("is_listing", 0))

            # Update highest price
            if price > highest:
                db.update_position(symbol, highest_price=price)
                highest = price

            # Update monotonic trailing stop
            new_trail = highest - config.trail_atr * (entry * 0.02)  # rough ATR proxy
            if new_trail > trail:
                db.update_position(symbol, trail_price=new_trail)
                trail = new_trail

            # Listing play: tight 3% stop, max 15min
            if is_listing:
                entry_ts = pos.get("entry_ts", "")
                if entry_ts:
                    try:
                        entry_time = datetime.fromisoformat(entry_ts)
                        if entry_time.tzinfo is None:
                            entry_time = entry_time.replace(tzinfo=timezone.utc)
                        age_min = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
                        if age_min >= config.listing_max_hold_min:
                            await self._close_position(symbol, qty, price, "listing_timeout")
                            continue
                    except Exception:
                        pass
                listing_stop = entry * (1 - config.listing_stop_pct / 100)
                if price <= listing_stop:
                    await self._close_position(symbol, qty, price, "listing_stop")
                    continue

            # Standard stop loss
            if stop > 0 and price <= stop:
                await self._close_position(symbol, qty, price, "stop_loss")
                continue

            # Partial take profit (50% at TP)
            if not partial_sold and tp > 0 and price >= tp:
                partial_qty = qty * 0.5
                await self._partial_sell(symbol, partial_qty, price)
                remaining_qty = qty - partial_qty
                db.update_position(symbol, qty=remaining_qty, partial_sold=1)
                logger.info(f"Partial TP hit: {symbol} sold {partial_qty:.4f} at {price}")

            # Trailing stop (for remaining position)
            if trail > 0 and price <= trail and partial_sold:
                remaining_qty = db.get_position(symbol)
                if remaining_qty:
                    await self._close_position(symbol, remaining_qty["qty"], price, "trail_stop")
                continue

            # Force exit after max hold time
            entry_ts = pos.get("entry_ts", "")
            if entry_ts:
                try:
                    entry_time = datetime.fromisoformat(entry_ts)
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=timezone.utc)
                    age_h = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
                    if age_h >= config.max_hold_hours:
                        await self._close_position(symbol, qty, price, "timeout")
                except Exception:
                    pass

    async def _close_position(self, symbol: str, qty: float, price: float, reason: str) -> None:
        pos = db.get_position(symbol)
        if not pos:
            return
        entry_price = pos["entry_price"]
        pnl = (price - entry_price) * qty
        tier = await intelligence.get_tier()

        try:
            await client.market_sell(symbol, qty)
        except BinanceError as e:
            logger.error(f"Sell error {symbol}: {e}")
            return

        db.record_trade(
            symbol=symbol, action="sell", price=price, qty=qty,
            pnl=pnl, reason=reason, tier=tier, is_paper=config.paper_mode,
        )
        db.delete_position(symbol)

        emoji = "✅" if pnl > 0 else "❌"
        await notify.send(
            f"{emoji} CLOSED {symbol}\n"
            f"Entry: {entry_price:.4f} → Exit: {price:.4f}\n"
            f"PnL: {pnl:+.2f} USDC | Reason: {reason}"
        )
        logger.info(f"Position closed: {symbol} pnl={pnl:+.2f} reason={reason}")

        # Update consecutive loss counter
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        if self._consecutive_losses >= 3:
            self.pause(duration_sec=7200)
            await notify.send("⚠️ 3 consecutive losses — pausing 2h")
            self._consecutive_losses = 0

    async def _partial_sell(self, symbol: str, qty: float, price: float) -> None:
        try:
            await client.market_sell(symbol, qty)
            db.record_trade(
                symbol=symbol, action="partial_sell", price=price, qty=qty,
                pnl=0.0, reason="partial_tp", is_paper=config.paper_mode,
            )
        except BinanceError as e:
            logger.error(f"Partial sell error {symbol}: {e}")

    # ── Signal scoring ────────────────────────────────────────────────────────

    async def _build_signal(
        self,
        symbol: str,
        source: str,
        pump_zscore: float = 0.0,
        pool: str = "safe",
    ) -> TradeSignal | None:
        """Build a full TradeSignal for a symbol. Returns None if not enough data."""
        try:
            # Fetch all timeframes concurrently
            candles_5m, candles_1h, candles_4h, ticker = await asyncio.gather(
                client.klines(symbol, "5m",  limit=60),
                client.klines(symbol, "1h",  limit=60),
                client.klines(symbol, "4h",  limit=60),
                client.ticker_24h(symbol),
            )
            if len(candles_5m) < 30:
                return None

            # Use 5m for base indicators (most granular)
            closes  = [c["close"]  for c in candles_5m]
            highs   = [c["high"]   for c in candles_5m]
            lows    = [c["low"]    for c in candles_5m]
            volumes = [c["volume"] for c in candles_5m]

            price = closes[-1]
            tf = ind.timeframe_signal(
                closes, highs, lows, volumes,
                config.rsi_oversold, config.rsi_overbought,
            )

            # Multi-timeframe confluence
            mtf = ind.multi_timeframe_confluence(
                candles_5m, candles_1h, candles_4h,
                config.rsi_oversold, config.rsi_overbought,
            )

            vol_24h = float(ticker.get("quoteVolume", 0))

            ob = await order_book.analyze(symbol, vol_24h)
            senti = await sent.get_sentiment(symbol)

        except Exception as e:
            logger.warning(f"Signal build error {symbol}: {e}")
            return None

        return TradeSignal(
            symbol=symbol,
            source=source,
            score=0.0,  # filled below
            price=price,
            volume_24h=vol_24h,
            rsi=tf.rsi_val,
            ema_trend=tf.ema_trend,
            bb_pct=tf.bb_pct,
            vol_ratio=tf.vol_ratio,
            ob_imbalance=ob.imbalance,
            whale_bid=ob.whale_bid,
            fear_greed=senti.fear_greed,
            fear_greed_label=senti.fear_greed_label,
            pump_zscore=pump_zscore,
            pool=pool,
            mtf_score=mtf.score,
            mtf_direction=mtf.direction,
            mtf_agreement=mtf.agreement,
        )

    def _score_signal(self, sig: TradeSignal) -> float:
        """Pre-LLM heuristic score 0–100 with multi-timeframe confluence."""
        score = 0.0

        # ── Multi-timeframe confluence (biggest weight) ──────────────────────
        # MTF score is already -100..+100 weighted across 4h/1h/5m
        score += sig.mtf_score * 0.4   # up to ±40 points from MTF

        # Bonus for strong agreement across timeframes
        if sig.mtf_agreement >= 3:
            score += 15
        elif sig.mtf_agreement == 2:
            score += 5

        # Bearish MTF overrides — don't buy into a downtrend
        if sig.mtf_direction == "short":
            score -= 30
        elif sig.mtf_direction == "neutral" and sig.mtf_score < -5:
            score -= 10

        # ── RSI (5m base) ────────────────────────────────────────────────────
        if sig.rsi is not None:
            if sig.rsi < config.rsi_oversold:
                score += 20 * (1 - sig.rsi / config.rsi_oversold)
            elif sig.rsi > config.rsi_overbought:
                score -= 15

        # ── EMA trend (5m) ───────────────────────────────────────────────────
        if sig.ema_trend == "up":
            score += 10
        elif sig.ema_trend == "down":
            score -= 10

        # ── Bollinger position ───────────────────────────────────────────────
        if sig.bb_pct is not None:
            if sig.bb_pct < 0.2:
                score += 8
            elif sig.bb_pct > 0.8:
                score -= 5

        # ── Volume ratio ─────────────────────────────────────────────────────
        if sig.vol_ratio is not None:
            if sig.vol_ratio >= config.volume_ratio_threshold:
                score += 15
            elif sig.vol_ratio < 0.5:
                score -= 8

        # ── Order book ───────────────────────────────────────────────────────
        if sig.ob_imbalance >= config.ob_imbalance_buy:
            score += 12
        elif sig.ob_imbalance <= config.ob_imbalance_sell:
            score -= 12

        # ── Whale bid ────────────────────────────────────────────────────────
        if sig.whale_bid:
            score += 8

        # ── Source bonus ─────────────────────────────────────────────────────
        if sig.source == "pump":
            score += sig.pump_zscore * 3
        elif sig.source == "listing":
            score += 30

        return max(0.0, min(100.0, score))

    # ── Entry logic ───────────────────────────────────────────────────────────

    async def _evaluate_entry(self, sig: TradeSignal, portfolio_value: float) -> bool:
        """Full evaluation pipeline: score → LLM → execute."""
        sig.score = self._score_signal(sig)

        # Pre-filter: skip low-score signals (no LLM call)
        if sig.source not in ("listing",) and sig.score < 30:
            db.save_signal(sig.symbol, sig.score, "skip", sig.source, acted_on=False)
            return False

        # Check position limits
        positions = db.get_positions()
        if len(positions) >= config.max_positions:
            return False

        # Check if already in position
        if db.get_position(sig.symbol):
            return False

        # LLM validation
        llm_input = {
            "symbol": sig.symbol,
            "price": sig.price,
            "rsi": sig.rsi,
            "ema_trend": sig.ema_trend,
            "bb_pct": sig.bb_pct,
            "vol_ratio": sig.vol_ratio,
            "ob_imbalance": sig.ob_imbalance,
            "whale_bid": sig.whale_bid,
            "fear_greed": sig.fear_greed,
            "fear_greed_label": sig.fear_greed_label,
            "source": sig.source,
            "pump_zscore": sig.pump_zscore,
            "mtf_score": sig.mtf_score,
            "mtf_direction": sig.mtf_direction,
            "mtf_agreement": f"{sig.mtf_agreement}/3 timeframes",
        }
        validation: LLMValidation = await intelligence.validate(llm_input)

        db.save_signal(
            sig.symbol, sig.score, validation.action, sig.source,
            data={"llm_conf": validation.confidence, "reason": validation.reason},
            acted_on=validation.action == "buy",
        )

        if validation.action != "buy" or validation.confidence < 0.5:
            logger.info(f"LLM skip {sig.symbol}: {validation.reason}")
            return False

        # Execute buy
        await self._execute_entry(sig, portfolio_value, validation)
        return True

    async def _execute_entry(
        self,
        sig: TradeSignal,
        portfolio_value: float,
        validation: LLMValidation,
    ) -> None:
        quote_qty = self._position_size(sig.pool, portfolio_value)
        if quote_qty < 5:   # minimum $5
            logger.warning(f"Position size too small: {quote_qty:.2f} USDC")
            return

        try:
            result = await client.market_buy(sig.symbol, quote_qty)
        except BinanceError as e:
            logger.error(f"Buy error {sig.symbol}: {e}")
            return

        if config.paper_mode:
            exec_price = result["price"]
            qty = result["qty"]
        else:
            exec_price = float(result.get("fills", [{}])[0].get("price", sig.price) if result.get("fills") else sig.price)
            qty = float(result.get("executedQty", quote_qty / sig.price))

        # Calculate ATR-based stops
        try:
            candles = await client.klines(sig.symbol, "15m", limit=20)
            closes = [c["close"] for c in candles]
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            atr_val = ind.atr(highs, lows, closes) or (exec_price * 0.015)
        except Exception:
            atr_val = exec_price * 0.015

        stop = exec_price - config.stop_loss_atr * atr_val
        tp = exec_price + config.take_profit_atr * atr_val

        db.save_position(
            symbol=sig.symbol,
            entry_price=exec_price,
            qty=qty,
            pool=sig.pool,
            stop_loss=stop,
            take_profit=tp,
            is_listing=(sig.source == "listing"),
        )
        db.record_trade(
            symbol=sig.symbol, action="buy", price=exec_price, qty=qty,
            confidence=validation.confidence, tier=validation.tier,
            is_paper=config.paper_mode,
            reason=f"{sig.source}: {validation.reason[:80]}",
        )

        await notify.send(
            f"🟢 BUY {sig.symbol}\n"
            f"Price: {exec_price:.4f} | Qty: {qty:.4f}\n"
            f"Stop: {stop:.4f} | TP: {tp:.4f}\n"
            f"Source: {sig.source} | LLM conf: {validation.confidence:.0%}\n"
            f"Tier: {validation.tier} | {validation.reason[:80]}"
        )
        logger.info(f"Entry executed: {sig.symbol} @ {exec_price:.4f}")

    # ── ZeroClaw IPC ──────────────────────────────────────────────────────────

    def _write_status_file(self) -> None:
        """Write live status JSON to ZeroClaw workspace so pitonybot can read it."""
        try:
            positions = db.get_positions()
            trades_today = db.get_trades(limit=20)
            today = datetime.now(timezone.utc).date().isoformat()
            trades_today = [t for t in trades_today if t["ts"].startswith(today)]

            signals = db.get_recent_signals(10)
            perf    = db.get_performance(7)

            status = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "running": self._running,
                "paused": self.is_paused,
                "mode": self._mode,
                "paper_mode": config.paper_mode,
                "open_positions": len(positions),
                "today_pnl": round(db.get_today_pnl(), 2),
                "consecutive_losses": self._consecutive_losses,
                "positions": [
                    {
                        "symbol": p["symbol"],
                        "pool": p.get("pool", "?"),
                        "entry": round(p["entry_price"], 6),
                        "stop": round(p["stop_loss"], 6),
                        "tp": round(p["take_profit"], 6),
                        "qty": round(p["qty"], 4),
                        "entry_ts": p["entry_ts"],
                    }
                    for p in positions
                ],
                "recent_trades": [
                    {
                        "symbol": t["symbol"],
                        "action": t["action"],
                        "price": round(t["price"], 6),
                        "pnl": round(t["pnl"], 3),
                        "reason": t.get("reason", ""),
                        "ts": t["ts"],
                    }
                    for t in trades_today[:10]
                ],
                "recent_signals": [
                    {
                        "symbol": s["symbol"],
                        "score": round(s["score"], 1),
                        "action": s["action"],
                        "source": s["source"],
                        "acted_on": bool(s["acted_on"]),
                        "ts": s["ts"],
                    }
                    for s in signals[:8]
                ],
                "performance_7d": [
                    {"date": p["date"], "pnl": round(p["total_pnl"], 2),
                     "win_rate": round(p["win_rate"], 1), "trades": p["trades_count"]}
                    for p in perf
                ],
                "config": {
                    "rsi_oversold":  config.rsi_oversold,
                    "rsi_overbought": config.rsi_overbought,
                    "vol_ratio":     config.volume_ratio_threshold,
                    "max_positions": config.max_positions,
                    "risk_per_trade": f"{config.risk_per_trade:.0%}",
                    "daily_loss_limit": f"{config.daily_loss_limit:.0%}",
                },
            }

            _ZEROCLAW_WORKSPACE.mkdir(parents=True, exist_ok=True)
            _STATUS_FILE.write_text(json.dumps(status, indent=2))
        except Exception as e:
            logger.debug(f"Status file write error: {e}")

    def _check_cmd_file(self) -> None:
        """Process command from ZeroClaw (written by pitonybot skill)."""
        if not _CMD_FILE.exists():
            return
        try:
            raw = _CMD_FILE.read_text().strip()
            _CMD_FILE.unlink(missing_ok=True)   # consume immediately
            if not raw:
                return
            cmd = json.loads(raw)
            action = cmd.get("action", "").lower()

            if action == "pause":
                duration = int(cmd.get("duration", 3600))
                self.pause(duration_sec=duration)
                notify.send_sync(f"⏸️ Engine paused {duration//60}min via pitonybot")
                logger.info(f"CMD: pause {duration}s")

            elif action == "resume":
                self.resume()
                notify.send_sync("▶️ Engine resumed via pitonybot")
                logger.info("CMD: resume")

            elif action == "stop":
                self.stop()
                notify.send_sync("🛑 Engine stopped via pitonybot")
                logger.info("CMD: stop")

            elif action == "safe_only":
                self.set_mode("safe_only")
                notify.send_sync("🔒 Engine switched to safe_only mode via pitonybot")
                logger.info("CMD: safe_only")

            elif action == "full":
                self.set_mode("full")
                notify.send_sync("✅ Engine switched to full mode via pitonybot")
                logger.info("CMD: full")

            else:
                logger.warning(f"Unknown CMD action: {action}")

        except Exception as e:
            logger.warning(f"CMD file parse error: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _cycle(self) -> None:
        # Check ZeroClaw commands first
        self._check_cmd_file()

        if self.is_paused:
            self._write_status_file()
            return
        if not self._check_daily_loss():
            return

        portfolio_value = await self._get_portfolio_value()

        # 1. Manage open positions
        await self._manage_positions()

        # 2. Check pump candidates
        if self._mode == "full":
            pumps = pump_detector.get_candidates(min_score=config.pump_volume_zscore)
            for pump in pumps[:3]:  # max 3 pump candidates per cycle
                if db.get_position(pump.symbol):
                    continue
                sig = await self._build_signal(
                    pump.symbol, source="pump",
                    pump_zscore=pump.volume_zscore, pool="aggressive",
                )
                if sig:
                    await self._evaluate_entry(sig, portfolio_value)

        # 3. Check new listings
        if self._mode == "full":
            new_listings = listing_detector.get_recent(max_age_seconds=120)
            for listing in new_listings:
                if db.get_position(listing.symbol):
                    continue
                sig = await self._build_signal(
                    listing.symbol, source="listing",
                    pump_zscore=0.0, pool="aggressive",
                )
                if sig:
                    await self._evaluate_entry(sig, portfolio_value)

        # 4. Standard signals — safe pool (BTC, ETH, SOL, XRP)
        for symbol in config.safe_symbols:
            if db.get_position(symbol):
                continue
            sig = await self._build_signal(symbol, source="standard", pool="safe")
            if sig:
                await self._evaluate_entry(sig, portfolio_value)

        # 5. Standard signals — aggressive pool (SUI, NEAR, DOGE, PEPE)
        if self._mode == "full":
            for symbol in config.aggr_symbols:
                if db.get_position(symbol):
                    continue
                sig = await self._build_signal(symbol, source="standard", pool="aggressive")
                if sig:
                    await self._evaluate_entry(sig, portfolio_value)

        # Write status snapshot for ZeroClaw/pitonybot
        self._write_status_file()

    # ── WebSocket real-time position monitor ──────────────────────────────────

    def _on_ws_price(self, data: dict) -> None:
        """Callback from bookTicker WebSocket — fires on every best bid/ask update."""
        symbol = data.get("s")
        if not symbol:
            return
        bid = float(data.get("b", 0) or 0)
        ask = float(data.get("a", 0) or 0)
        if not bid and not ask:
            return
        price = (bid + ask) / 2 if bid and ask else (bid or ask)
        self._ws_prices[symbol] = price

    async def _ws_position_monitor(self) -> None:
        """
        Real-time stop/TP checker via WebSocket bookTicker.
        Runs alongside the 60s cycle — triggers exits instantly when price hits levels.
        """
        logger.info("WS position monitor started")
        while not self._stop_event.is_set():
            positions = db.get_positions()
            if not positions:
                await asyncio.sleep(5)
                continue

            symbols = [p["symbol"] for p in positions]
            logger.debug(f"WS monitoring {symbols}")

            try:
                ws_stop = asyncio.Event()

                async def _run_ws():
                    await client.book_ticker_stream(symbols, self._on_ws_price, ws_stop)

                ws_task = asyncio.create_task(_run_ws())

                # Check prices from WS every 500ms
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.5)
                    current_symbols = {p["symbol"] for p in db.get_positions()}
                    if current_symbols != set(symbols):
                        break  # positions changed — reconnect with new symbols

                    for pos in db.get_positions():
                        sym = pos["symbol"]
                        price = self._ws_prices.get(sym)
                        if price is None:
                            continue
                        stop = pos["stop_loss"]
                        tp   = pos["take_profit"]
                        trail = pos["trail_price"]
                        highest = pos["highest_price"]
                        partial_sold = bool(pos["partial_sold"])

                        # Update highest + trailing stop
                        if price > highest:
                            atr_proxy = pos["entry_price"] * 0.015
                            new_trail = price - config.trail_atr * atr_proxy
                            if new_trail > trail:
                                db.update_position(sym, highest_price=price, trail_price=new_trail)

                        # Stop loss
                        if stop > 0 and price <= stop:
                            logger.info(f"WS stop triggered: {sym} @ {price:.6f} (stop={stop:.6f})")
                            asyncio.create_task(
                                self._close_position(sym, pos["qty"], price, "ws_stop_loss")
                            )

                        # Take profit (partial)
                        elif not partial_sold and tp > 0 and price >= tp:
                            partial_qty = pos["qty"] * 0.5
                            logger.info(f"WS TP triggered: {sym} @ {price:.6f} (tp={tp:.6f})")
                            asyncio.create_task(self._partial_sell(sym, partial_qty, price))
                            db.update_position(sym, partial_sold=1, qty=pos["qty"] - partial_qty)

                        # Trail stop (after partial sell)
                        elif partial_sold and trail > 0 and price <= trail:
                            logger.info(f"WS trail stop: {sym} @ {price:.6f} (trail={trail:.6f})")
                            asyncio.create_task(
                                self._close_position(sym, pos["qty"], price, "ws_trail_stop")
                            )

                ws_stop.set()
                ws_task.cancel()
                await asyncio.sleep(1)

            except Exception as e:
                logger.warning(f"WS monitor error: {e} — retrying in 5s")
                await asyncio.sleep(5)

    async def start(self) -> None:
        self._running = True
        self._ws_prices: dict[str, float] = {}
        logger.info("TradingEngine started")

        # Start WebSocket position monitor as background task
        ws_task = asyncio.create_task(self._ws_position_monitor())

        while not self._stop_event.is_set():
            t0 = time.time()
            try:
                await self._cycle()
            except Exception as e:
                logger.error(f"Engine cycle error: {e}", exc_info=True)
            elapsed = time.time() - t0
            sleep_time = max(0.0, 60.0 - elapsed)
            try:
                await asyncio.wait_for(asyncio.sleep(sleep_time), timeout=sleep_time + 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                break

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def status(self) -> dict:
        positions = db.get_positions()
        today_pnl = db.get_today_pnl()
        return {
            "running": self._running,
            "paused": self.is_paused,
            "mode": self._mode,
            "open_positions": len(positions),
            "today_pnl": round(today_pnl, 2),
            "consecutive_losses": self._consecutive_losses,
            "paper_mode": config.paper_mode,
        }


# Singleton
engine = TradingEngine()
