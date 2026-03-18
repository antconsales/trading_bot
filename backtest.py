"""Pi Trader — Backtester.

Fetches real 30-day OHLCV from Binance, simulates the signal-scoring
logic and ATR-based stop/TP exits, and reports win rate + optimal thresholds.

Usage:
    python backtest.py                 # runs on all_symbols, 30 days, 1h candles
    python backtest.py BTCUSDC 60      # 60-day backtest on BTC
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# ── Bootstrap path so we can import pi_trader modules ─────────────────────────
import os
sys.path.insert(0, os.path.dirname(__file__))

from config import config
import indicators as ind

logger = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# ── Constants ──────────────────────────────────────────────────────────────────
BINANCE_REST = "https://api.binance.com"
KLINES_LIMIT = 1000   # max per request

ENTRY_SCORE_MIN = 55.0   # matches engine default

# Slippage model: Binance taker fee (0.1%) + market impact (~0.05%)
# Applied per side — round-trip cost ~0.3%
SLIPPAGE_BUY  = 0.0010   # entry fills 0.10% above signal close
SLIPPAGE_SELL = 0.0010   # exit fills 0.10% below signal close


# Grid of thresholds to test
RSI_OVERSOLD_RANGE  = [25, 30, 33, 35, 38, 40]
RSI_OB_RANGE        = [60, 62, 65, 68, 70, 75]
VOL_RATIO_RANGE     = [1.5, 2.0, 2.5, 3.0]
STOP_ATR_RANGE      = [1.0, 1.5, 2.0]
TP_ATR_RANGE        = [1.5, 2.0, 2.5, 3.0]


# ── Binance fetch ──────────────────────────────────────────────────────────────

async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    days: int,
) -> list[dict]:
    """Fetch up to `days` worth of klines for `symbol`/`interval`."""
    ms_per_candle = {
        "5m": 5 * 60_000, "15m": 15 * 60_000,
        "1h": 60 * 60_000, "4h": 4 * 3_600_000,
    }.get(interval, 60 * 60_000)

    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    all_candles: list[dict] = []
    current_start = start_ms

    while current_start < end_ms:
        url = (
            f"{BINANCE_REST}/api/v3/klines"
            f"?symbol={symbol}&interval={interval}"
            f"&startTime={current_start}&endTime={end_ms}&limit={KLINES_LIMIT}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    break
                raw = await r.json()
        except Exception as e:
            logger.warning(f"fetch_klines {symbol}/{interval}: {e}")
            break

        if not raw:
            break

        for c in raw:
            all_candles.append({
                "open_time": c[0],
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })

        last_time = raw[-1][0]
        if last_time <= current_start:
            break
        current_start = last_time + ms_per_candle

    return all_candles


# ── Signal scoring (mirrors engine._score_signal logic) ───────────────────────

@dataclass
class BacktestParams:
    rsi_oversold: float  = 35.0
    rsi_overbought: float = 65.0
    vol_ratio_threshold: float = 2.5
    stop_atr_mult: float = 1.5
    tp_atr_mult: float   = 2.0


def _score_candle(
    idx: int,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    params: BacktestParams,
    candles_1h: list[dict],
    candles_4h: list[dict],
) -> float:
    """Score a single 5m candle index. Returns -100..+100."""
    if idx < 60:
        return 0.0

    c5m_slice  = closes[max(0, idx - 59): idx + 1]
    h5m_slice  = highs[max(0, idx - 59): idx + 1]
    l5m_slice  = lows[max(0, idx - 59): idx + 1]
    v5m_slice  = volumes[max(0, idx - 59): idx + 1]

    # Find corresponding 1h and 4h candle indices
    ot_5m = idx  # approximate using index

    # Build minimal candle dicts for MTF
    def _make_candles(cs, hs, ls, vs):
        return [{"close": c, "high": h, "low": l, "volume": v, "open": c}
                for c, h, l, v in zip(cs, hs, ls, vs)]

    mtf = ind.multi_timeframe_confluence(
        _make_candles(c5m_slice, h5m_slice, l5m_slice, v5m_slice),
        candles_1h[-60:] if len(candles_1h) >= 60 else candles_1h,
        candles_4h[-60:] if len(candles_4h) >= 60 else candles_4h,
        rsi_oversold=params.rsi_oversold,
        rsi_overbought=params.rsi_overbought,
    )

    score = 0.0

    # MTF (40%)
    score += mtf.score * 0.4
    if mtf.agreement >= 3:
        score += 15
    if mtf.direction == "short":
        score -= 30

    # Single-TF indicators
    rsi_v = ind.rsi(c5m_slice, 14)
    bb    = ind.bollinger(c5m_slice, 20)
    vr    = ind.volume_ratio(v5m_slice, 20)

    if rsi_v is not None:
        if rsi_v < params.rsi_oversold:
            score += 20 * (1 - rsi_v / params.rsi_oversold)
        elif rsi_v > params.rsi_overbought:
            score -= 20

    if bb:
        if bb.pct_b < 0.2:
            score += 12
        elif bb.pct_b > 0.8:
            score -= 10
        if bb.width < 0.03:
            score += 8

    if vr is not None:
        if vr >= params.vol_ratio_threshold:
            score += 12 * min(vr / 3.0, 1.0)
        elif vr < 0.5:
            score -= 5

    return max(-100.0, min(100.0, score))


# ── Simulate trades ────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    entry: float
    exit: float
    pnl_pct: float
    bars_held: int
    won: bool


def simulate_trades(
    candles_5m: list[dict],
    candles_1h: list[dict],
    candles_4h: list[dict],
    params: BacktestParams,
    entry_score_min: float = ENTRY_SCORE_MIN,
) -> list[TradeResult]:
    closes  = [c["close"]  for c in candles_5m]
    highs   = [c["high"]   for c in candles_5m]
    lows    = [c["low"]    for c in candles_5m]
    volumes = [c["volume"] for c in candles_5m]

    results: list[TradeResult] = []
    in_trade = False
    entry_price = 0.0
    stop_price  = 0.0
    tp_price    = 0.0
    entry_bar   = 0
    max_hold    = int(config.max_hold_hours * 12)   # 4h @ 5m bars

    for i in range(60, len(closes)):
        if not in_trade:
            score = _score_candle(i, closes, highs, lows, volumes, params, candles_1h, candles_4h)
            if score >= entry_score_min:
                atr_v = ind.atr(highs[:i+1], lows[:i+1], closes[:i+1], 14)
                if atr_v is None or atr_v == 0:
                    continue
                entry_price = closes[i] * (1 + SLIPPAGE_BUY)   # slippage on entry
                stop_price  = entry_price - params.stop_atr_mult * atr_v
                tp_price    = entry_price + params.tp_atr_mult   * atr_v
                entry_bar   = i
                in_trade    = True
        else:
            price = closes[i]
            bars  = i - entry_bar

            if price <= stop_price:
                exit_price = stop_price * (1 - SLIPPAGE_SELL)  # slippage on stop
                pnl = (exit_price - entry_price) / entry_price * 100
                results.append(TradeResult(entry_price, exit_price, pnl, bars, False))
                in_trade = False
            elif price >= tp_price:
                exit_price = tp_price * (1 - SLIPPAGE_SELL)    # slippage on TP
                pnl = (exit_price - entry_price) / entry_price * 100
                results.append(TradeResult(entry_price, exit_price, pnl, bars, True))
                in_trade = False
            elif bars >= max_hold:
                exit_price = price * (1 - SLIPPAGE_SELL)        # slippage on timeout
                pnl = (exit_price - entry_price) / entry_price * 100
                results.append(TradeResult(entry_price, exit_price, pnl, bars, pnl > 0))
                in_trade = False

    return results


# ── Grid search ───────────────────────────────────────────────────────────────

def grid_search(
    candles_5m: list[dict],
    candles_1h: list[dict],
    candles_4h: list[dict],
    min_trades: int = 10,
) -> Optional[BacktestParams]:
    """Try all parameter combinations, return best by Sharpe-like score."""
    best_params = None
    best_metric = float("-inf")
    best_stats  = {}

    for rsi_os in RSI_OVERSOLD_RANGE:
        for rsi_ob in RSI_OB_RANGE:
            if rsi_ob <= rsi_os + 20:
                continue
            for vol_r in VOL_RATIO_RANGE:
                for stop_a in STOP_ATR_RANGE:
                    for tp_a in TP_ATR_RANGE:
                        if tp_a <= stop_a:
                            continue
                        p = BacktestParams(rsi_os, rsi_ob, vol_r, stop_a, tp_a)
                        trades = simulate_trades(candles_5m, candles_1h, candles_4h, p)
                        if len(trades) < min_trades:
                            continue

                        wins    = sum(1 for t in trades if t.won)
                        total   = len(trades)
                        win_rate = wins / total
                        avg_pnl  = sum(t.pnl_pct for t in trades) / total
                        pos_pnl  = [t.pnl_pct for t in trades if t.won]
                        neg_pnl  = [t.pnl_pct for t in trades if not t.won]
                        avg_win  = sum(pos_pnl) / len(pos_pnl) if pos_pnl else 0
                        avg_loss = sum(neg_pnl) / len(neg_pnl) if neg_pnl else 0

                        # Expect value * win_rate bonus
                        ev = avg_pnl
                        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 1
                        metric = ev * (1 + win_rate) * min(rr, 3.0)

                        if metric > best_metric:
                            best_metric = metric
                            best_params = p
                            best_stats  = {
                                "trades": total,
                                "win_rate": round(win_rate * 100, 1),
                                "avg_pnl_pct": round(avg_pnl, 3),
                                "avg_win": round(avg_win, 3),
                                "avg_loss": round(avg_loss, 3),
                                "rr_ratio": round(rr, 2),
                                "metric": round(metric, 4),
                            }

    if best_params:
        logger.info(f"Best params: {best_params} → stats: {best_stats}")
    return best_params


# ── Per-symbol report ─────────────────────────────────────────────────────────

async def backtest_symbol(
    session: aiohttp.ClientSession,
    symbol: str,
    days: int,
) -> dict:
    logger.info(f"Backtesting {symbol} ({days}d)...")

    candles_5m, candles_1h, candles_4h = await asyncio.gather(
        fetch_klines(session, symbol, "5m",  days),
        fetch_klines(session, symbol, "1h",  days),
        fetch_klines(session, symbol, "4h",  days),
    )

    if len(candles_5m) < 100:
        logger.warning(f"{symbol}: not enough 5m data ({len(candles_5m)} candles)")
        return {"symbol": symbol, "error": "insufficient_data"}

    logger.info(f"  {symbol}: {len(candles_5m)} 5m, {len(candles_1h)} 1h, {len(candles_4h)} 4h candles")

    best = grid_search(candles_5m, candles_1h, candles_4h)

    if best is None:
        # Too few trades with default params — try default
        default = BacktestParams()
        trades  = simulate_trades(candles_5m, candles_1h, candles_4h, default)
        logger.info(f"  {symbol}: no grid winner, default params → {len(trades)} trades")
        return {
            "symbol": symbol,
            "best_params": vars(default),
            "trades": len(trades),
            "note": "fallback_default",
        }

    return {
        "symbol": symbol,
        "best_params": vars(best),
    }


# ── Aggregate & save ───────────────────────────────────────────────────────────

def _aggregate_params(results: list[dict]) -> dict:
    """Median of best params across symbols."""
    rsi_os   = sorted(r["best_params"]["rsi_oversold"]   for r in results if "best_params" in r)
    rsi_ob   = sorted(r["best_params"]["rsi_overbought"] for r in results if "best_params" in r)
    vol_r    = sorted(r["best_params"]["vol_ratio_threshold"] for r in results if "best_params" in r)
    stop_a   = sorted(r["best_params"]["stop_atr_mult"]  for r in results if "best_params" in r)
    tp_a     = sorted(r["best_params"]["tp_atr_mult"]    for r in results if "best_params" in r)

    def med(lst):
        if not lst:
            return None
        mid = len(lst) // 2
        return lst[mid]

    return {
        "rsi_oversold":        med(rsi_os),
        "rsi_overbought":      med(rsi_ob),
        "vol_ratio_threshold": med(vol_r),
        "stop_atr_mult":       med(stop_a),
        "tp_atr_mult":         med(tp_a),
    }


async def run_backtest(symbols: list[str] | None = None, days: int = 30) -> dict:
    if symbols is None:
        symbols = list(config.all_symbols)

    async with aiohttp.ClientSession() as session:
        tasks = [backtest_symbol(session, s, days) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    clean = []
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"backtest error: {r}")
        elif isinstance(r, dict):
            clean.append(r)

    agg = _aggregate_params(clean)

    report = {
        "symbols": clean,
        "aggregate": agg,
        "days": days,
        "generated_at": int(time.time()),
    }

    report_path = os.path.join(os.path.dirname(__file__), "backtest_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info(f"Report saved to {report_path}")

    # Apply to live config (autotuner-style)
    if agg.get("rsi_oversold"):
        config.rsi_oversold          = agg["rsi_oversold"]
        config.rsi_overbought        = agg["rsi_overbought"]
        config.volume_ratio_threshold= agg["vol_ratio_threshold"]
        config.stop_loss_atr         = agg["stop_atr_mult"]
        config.take_profit_atr       = agg["tp_atr_mult"]
        logger.info(f"Live config updated: {agg}")

    return report


# ── CLI entry ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    syms = [args[0].upper()] if args else None
    days = int(args[1]) if len(args) >= 2 else 30

    report = asyncio.run(run_backtest(syms, days))
    agg = report.get("aggregate", {})
    print("\n=== BACKTEST RESULTS ===")
    print(f"Symbols:          {[r['symbol'] for r in report['symbols']]}")
    print(f"Period:           {days} days")
    print(f"Optimal RSI OS:   {agg.get('rsi_oversold')}")
    print(f"Optimal RSI OB:   {agg.get('rsi_overbought')}")
    print(f"Optimal Vol Ratio:{agg.get('vol_ratio_threshold')}")
    print(f"Optimal Stop ATR: {agg.get('stop_atr_mult')}x")
    print(f"Optimal TP ATR:   {agg.get('tp_atr_mult')}x")
    print("========================")
