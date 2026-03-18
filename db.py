"""Pi Trader — SQLite database.

Async-friendly via aiosqlite with a synchronous fallback for simple ops.
All timestamps stored as ISO-8601 UTC strings.
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH: str = ""
_conn: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,   -- buy, sell, partial_sell
    price       REAL NOT NULL,
    qty         REAL NOT NULL,
    pnl         REAL DEFAULT 0.0,
    reason      TEXT,
    confidence  REAL DEFAULT 0.0,
    tier        TEXT DEFAULT 'local',  -- local, amr5
    is_paper    INTEGER DEFAULT 1,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL UNIQUE,
    entry_price REAL NOT NULL,
    qty         REAL NOT NULL,
    pool        TEXT DEFAULT 'safe',   -- safe, aggressive
    stop_loss   REAL DEFAULT 0.0,
    take_profit REAL DEFAULT 0.0,
    trail_price REAL DEFAULT 0.0,
    highest_price REAL DEFAULT 0.0,
    partial_sold  INTEGER DEFAULT 0,
    original_qty  REAL DEFAULT 0.0,
    is_listing    INTEGER DEFAULT 0,
    entry_ts    TEXT NOT NULL,
    updated_ts  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    score       REAL DEFAULT 0.0,
    action      TEXT,              -- buy, skip
    source      TEXT,              -- pump, listing, standard, ob_whale
    data_json   TEXT,
    acted_on    INTEGER DEFAULT 0,
    ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL UNIQUE,
    total_pnl     REAL DEFAULT 0.0,
    win_rate      REAL DEFAULT 0.0,
    max_drawdown  REAL DEFAULT 0.0,
    trades_count  INTEGER DEFAULT 0,
    ts            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_store (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init(db_path: str) -> None:
    global _DB_PATH, _conn
    _DB_PATH = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    # Migrations
    try:
        _conn.execute("ALTER TABLE positions ADD COLUMN side TEXT DEFAULT 'long'")
        _conn.commit()
        logger.info("Migration: added side column to positions")
    except Exception:
        pass  # Column already exists
    _conn.commit()
    logger.info(f"SQLite DB initialized: {db_path}")


def _db() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db.init() not called")
    return _conn


# ── Trades ────────────────────────────────────────────────────────────────────

def record_trade(
    symbol: str,
    action: str,
    price: float,
    qty: float,
    pnl: float = 0.0,
    reason: str = "",
    confidence: float = 0.0,
    tier: str = "local",
    is_paper: bool = True,
) -> int:
    c = _db().execute(
        """INSERT INTO trades (symbol, action, price, qty, pnl, reason, confidence, tier, is_paper, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, action, price, qty, pnl, reason, confidence, tier, int(is_paper), _now()),
    )
    _db().commit()
    return c.lastrowid or 0


def get_trades(limit: int = 50, symbol: str | None = None) -> list[dict]:
    if symbol:
        rows = _db().execute(
            "SELECT * FROM trades WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit)
        ).fetchall()
    else:
        rows = _db().execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_pnl() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    row = _db().execute(
        "SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE ts LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return float(row["total"]) if row else 0.0


def get_trade_streak() -> int:
    """Return consecutive losses (negative) or wins (positive)."""
    rows = _db().execute(
        "SELECT pnl FROM trades ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return 0
    streak = 0
    sign = 1 if rows[0]["pnl"] >= 0 else -1
    for r in rows:
        if (r["pnl"] >= 0 and sign == 1) or (r["pnl"] < 0 and sign == -1):
            streak += sign
        else:
            break
    return streak


# ── Positions ─────────────────────────────────────────────────────────────────

def save_position(
    symbol: str,
    entry_price: float,
    qty: float,
    pool: str = "safe",
    stop_loss: float = 0.0,
    take_profit: float = 0.0,
    is_listing: bool = False,
    side: str = "long",
) -> None:
    now = _now()
    _db().execute(
        """INSERT INTO positions
               (symbol, entry_price, qty, pool, stop_loss, take_profit,
                trail_price, highest_price, original_qty, is_listing, side, entry_ts, updated_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
               entry_price=excluded.entry_price,
               qty=excluded.qty,
               pool=excluded.pool,
               stop_loss=excluded.stop_loss,
               take_profit=excluded.take_profit,
               trail_price=excluded.trail_price,
               highest_price=excluded.highest_price,
               original_qty=excluded.original_qty,
               is_listing=excluded.is_listing,
               side=excluded.side,
               updated_ts=excluded.updated_ts""",
        (symbol, entry_price, qty, pool, stop_loss, take_profit,
         entry_price, entry_price, qty, int(is_listing), side, now, now),
    )
    _db().commit()


def update_position(symbol: str, **kwargs: Any) -> None:
    if not kwargs:
        return
    kwargs["updated_ts"] = _now()
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [symbol]
    _db().execute(f"UPDATE positions SET {sets} WHERE symbol=?", vals)
    _db().commit()


def delete_position(symbol: str) -> None:
    _db().execute("DELETE FROM positions WHERE symbol=?", (symbol,))
    _db().commit()


def get_positions() -> list[dict]:
    rows = _db().execute("SELECT * FROM positions ORDER BY entry_ts").fetchall()
    return [dict(r) for r in rows]


def get_position(symbol: str) -> dict | None:
    row = _db().execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    return dict(row) if row else None


# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(
    symbol: str,
    score: float,
    action: str,
    source: str,
    data: dict | None = None,
    acted_on: bool = False,
) -> None:
    _db().execute(
        """INSERT INTO signals (symbol, score, action, source, data_json, acted_on, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (symbol, score, action, source, json.dumps(data or {}), int(acted_on), _now()),
    )
    _db().commit()


def get_recent_signals(limit: int = 20) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Performance ───────────────────────────────────────────────────────────────

def upsert_performance(
    date: str,
    total_pnl: float,
    win_rate: float,
    max_drawdown: float,
    trades_count: int,
) -> None:
    _db().execute(
        """INSERT INTO performance (date, total_pnl, win_rate, max_drawdown, trades_count, ts)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               total_pnl=excluded.total_pnl,
               win_rate=excluded.win_rate,
               max_drawdown=excluded.max_drawdown,
               trades_count=excluded.trades_count,
               ts=excluded.ts""",
        (date, total_pnl, win_rate, max_drawdown, trades_count, _now()),
    )
    _db().commit()


def get_performance(days: int = 30) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM performance ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Config store ──────────────────────────────────────────────────────────────

def config_set(key: str, value: Any) -> None:
    _db().execute(
        """INSERT INTO config_store (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, json.dumps(value), _now()),
    )
    _db().commit()


def config_get(key: str, default: Any = None) -> Any:
    row = _db().execute(
        "SELECT value FROM config_store WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]
