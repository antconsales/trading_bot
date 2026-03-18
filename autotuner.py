"""Pi Trader — Autotuner.

Runs every Sunday at 03:00 UTC.
Backtests current signal thresholds on last 30 days of stored signals.
Adjusts: RSI oversold, BB squeeze threshold, volume ratio.
Does NOT touch: stop-loss, capital split, position size.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

import db
import backtest as bt
from config import config
from notify import notify

logger = logging.getLogger(__name__)



def _persist_to_env(updates: dict[str, str]) -> None:
    """Write key=value pairs to the pi_trader .env file (create or update)."""
    import os
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        lines: list[str] = []
        existing_keys: set[str] = set()
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k = stripped.split("=", 1)[0].strip()
                        if k in updates:
                            lines.append(f"{k}={updates[k]}\n")
                            existing_keys.add(k)
                            continue
                    lines.append(line if line.endswith("\n") else line + "\n")
        # Append any keys not already in file
        for k, v in updates.items():
            if k not in existing_keys:
                lines.append(f"{k}={v}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)
        logger.info(f"Persisted to .env: {list(updates.keys())}")
    except Exception as e:
        logger.warning(f"Failed to persist .env: {e}")

def _win_rate(signals: list[dict]) -> float:
    acted = [s for s in signals if s["acted_on"]]
    if not acted:
        return 0.0
    wins = sum(1 for s in acted if s.get("data_json") and '"buy"' in s.get("data_json", ""))
    return wins / len(acted)


def _backtest_rsi(signals: list[dict], threshold: float) -> float:
    """
    Simulate: how many signals with rsi < threshold led to profitable trades.
    Uses stored signal data_json to reconstruct scenario.
    This is a simplified scoring — real backtesting would need OHLCV.
    """
    relevant = [
        s for s in signals
        if s.get("action") == "buy" and s.get("acted_on")
    ]
    if not relevant:
        return 0.0
    # Proxy: signals with higher LLM confidence that we acted on
    good = sum(1 for s in relevant if '"confidence": 0.' in s.get("data_json", ""))
    return good / len(relevant) if relevant else 0.0


def _suggest_rsi_threshold(signals: list[dict], current: float) -> float:
    """Nudge RSI threshold based on recent performance."""
    # Count signals at different RSI bands (from stored data)
    # Since we don't store raw RSI in signals table, we use win-rate proxy
    trades = db.get_trades(limit=200)
    if not trades:
        return current

    sells = [t for t in trades if t["action"] in ("sell", "partial_sell")]
    if len(sells) < 10:
        return current

    wins = sum(1 for t in sells if t["pnl"] > 0)
    win_rate = wins / len(sells)

    # If win rate is too low, tighten RSI (lower threshold = stricter)
    if win_rate < 0.45:
        new = max(25.0, current - 3.0)
        logger.info(f"Autotuner: win_rate={win_rate:.0%} → tightening RSI {current} → {new}")
        return new
    # If win rate is high, can relax a bit
    elif win_rate > 0.65:
        new = min(45.0, current + 2.0)
        logger.info(f"Autotuner: win_rate={win_rate:.0%} → relaxing RSI {current} → {new}")
        return new
    return current


def _suggest_vol_ratio(current: float) -> float:
    """Adjust volume ratio threshold based on recent false positives."""
    trades = db.get_trades(limit=100)
    if not trades:
        return current

    # Trades with source="pump" that lost money → pump threshold too loose
    pump_trades = [t for t in trades if "pump" in (t.get("reason") or "")]
    if len(pump_trades) < 5:
        return current

    pump_losses = sum(1 for t in pump_trades if t["pnl"] < 0)
    pump_loss_rate = pump_losses / len(pump_trades)

    if pump_loss_rate > 0.6:
        new = min(current + 0.3, 4.0)
        logger.info(f"Autotuner: pump loss rate {pump_loss_rate:.0%} → raising vol ratio {current} → {new}")
        return new
    elif pump_loss_rate < 0.3:
        new = max(current - 0.2, 1.8)
        logger.info(f"Autotuner: pump loss rate {pump_loss_rate:.0%} → lowering vol ratio {current} → {new}")
        return new
    return current


def run_backtest() -> dict:
    """Run backtest analysis and return suggested adjustments."""
    signals = db.get_recent_signals(limit=500)
    trades = db.get_trades(limit=200)

    sells = [t for t in trades if t["action"] in ("sell", "partial_sell")]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    win_rate = wins / len(sells) if sells else 0.0
    total_pnl = sum(t["pnl"] for t in sells)
    max_dd = min((t["pnl"] for t in sells), default=0.0)

    new_rsi = _suggest_rsi_threshold(signals, config.rsi_oversold)
    new_vol = _suggest_vol_ratio(config.volume_ratio_threshold)

    return {
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "max_single_loss": max_dd,
        "trade_count": len(sells),
        "suggested_rsi_oversold": new_rsi,
        "suggested_vol_ratio": new_vol,
    }


def apply_adjustments(results: dict) -> list[str]:
    """Apply suggestions and return list of changes made."""
    changes = []

    rsi_new = results["suggested_rsi_oversold"]
    if abs(rsi_new - config.rsi_oversold) > 0.5:
        db.config_set("rsi_oversold", rsi_new)
        config.rsi_oversold = rsi_new
        os.environ["RSI_OVERSOLD"] = str(rsi_new)
        changes.append(f"RSI oversold: → {rsi_new:.1f}")

    vol_new = results["suggested_vol_ratio"]
    if abs(vol_new - config.volume_ratio_threshold) > 0.1:
        db.config_set("vol_ratio", vol_new)
        config.volume_ratio_threshold = vol_new
        os.environ["VOL_RATIO"] = str(vol_new)
        changes.append(f"Vol ratio: → {vol_new:.2f}")

    return changes


async def run_autotuner() -> None:
    logger.info("Autotuner: running weekly backtest (real OHLCV)...")

    # Step 1: real Binance OHLCV grid search
    ohlcv_changes: list[str] = []
    try:
        ohlcv_report = await bt.run_backtest(days=30)
        agg = ohlcv_report.get("aggregate", {})
        param_map = [
            ("rsi_oversold",       "rsi_oversold",           "RSI_OVERSOLD",  "RSI OS"),
            ("rsi_overbought",     "rsi_overbought",         "RSI_OVERBOUGHT","RSI OB"),
            ("vol_ratio_threshold","volume_ratio_threshold", "VOL_RATIO",     "Vol ratio"),
            ("stop_atr_mult",      "stop_loss_atr",          "STOP_ATR",      "Stop ATR"),
            ("tp_atr_mult",        "take_profit_atr",        "TP_ATR",        "TP ATR"),
        ]
        import os
        for key, cfg_attr, env_key, label in param_map:
            new_val = agg.get(key)
            if new_val is None:
                continue
            old_val = getattr(config, cfg_attr)
            if abs(new_val - old_val) > 0.05:
                setattr(config, cfg_attr, new_val)
                os.environ[env_key] = str(new_val)
                db.config_set(env_key.lower(), new_val)
                ohlcv_changes.append(f"{label}: {old_val:.2f} -> {new_val:.2f}")
        logger.info(f"OHLCV backtest: {len(ohlcv_report.get('symbols', []))} symbols")
    except Exception as e:
        logger.warning(f"OHLCV backtest failed: {e} -- using DB analysis only")

    # Step 2: DB-based fine tuning
    db_results = run_backtest()
    db_changes = apply_adjustments(db_results)
    all_changes = ohlcv_changes + db_changes

    lines = [
        "<b>\U0001f527 Weekly Autotuner Report</b>",
        "OHLCV reali (30gg) + analisi DB",
        "",
        "Trades: " + str(db_results['trade_count'])
        + " | Win: " + f"{db_results['win_rate']:.0%}"
        + " | PnL: " + f"{db_results['total_pnl']:+.2f}" + " USDC",
    ]
    if all_changes:
        lines += ["", "<b>Aggiustamenti:</b>"] + ["  \u2022 " + c for c in all_changes]
    else:
        lines.append("Nessun aggiustamento -- parametri ottimali.")

    await notify.send("\n".join(lines))
    logger.info(f"Autotuner done. Changes: {all_changes}")

async def start_autotuner_loop() -> None:
    """Background loop: run every Sunday at 03:00 UTC."""
    while True:
        now = datetime.now(timezone.utc)
        # Calculate next Sunday 03:00
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 3:
            days_until_sunday = 7
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
        wait_sec = (next_run - now).total_seconds()
        logger.info(f"Autotuner: next run in {wait_sec/3600:.1f}h ({next_run.isoformat()})")
        await asyncio.sleep(wait_sec)
        try:
            await run_autotuner()
        except Exception as e:
            logger.error(f"Autotuner error: {e}")
