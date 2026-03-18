"""Pi Trader — Configuration.

All settings loaded from environment variables / .env file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Load .env if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Config:
    # ── Binance ────────────────────────────────────────────────────────────
    binance_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    binance_api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))
    paper_mode: bool = field(default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() != "false")

    # ── Telegram ───────────────────────────────────────────────────────────
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # ── AMR5 bridge ────────────────────────────────────────────────────────
    amr5_host: str = field(default_factory=lambda: os.getenv("AMR5_HOST", "http://192.168.1.23"))
    amr5_ollama_port: int = field(default_factory=lambda: int(os.getenv("AMR5_OLLAMA_PORT", "11435")))
    amr5_model: str = field(default_factory=lambda: os.getenv("AMR5_MODEL", "qwen3:8b"))
    amr5_timeout: float = field(default_factory=lambda: float(os.getenv("AMR5_TIMEOUT", "2.0")))

    # ── Local LLM (Pi) ─────────────────────────────────────────────────────
    local_ollama_url: str = field(default_factory=lambda: os.getenv("LOCAL_OLLAMA_URL", "http://localhost:11434"))
    local_model: str = field(default_factory=lambda: os.getenv("LOCAL_MODEL", "qwen3.5:0.8b"))
    local_llm_timeout: float = field(default_factory=lambda: float(os.getenv("LOCAL_LLM_TIMEOUT", "30.0")))

    # ── Database ───────────────────────────────────────────────────────────
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", str(Path(__file__).parent / "pi_trader.db")))

    # ── Trading parameters ─────────────────────────────────────────────────
    quote_currency: str = "USDC"
    max_positions: int = field(default_factory=lambda: int(os.getenv("MAX_POSITIONS", "3")))
    risk_per_trade: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE", "0.02")))  # 2%
    safe_pool_ratio: float = field(default_factory=lambda: float(os.getenv("SAFE_POOL_RATIO", "0.70")))  # 70%
    daily_loss_limit: float = field(default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT", "0.05")))  # 5%
    max_hold_hours: float = field(default_factory=lambda: float(os.getenv("MAX_HOLD_HOURS", "4.0")))

    # ── Stop/TP multipliers (ATR-based) ────────────────────────────────────
    stop_loss_atr: float = 1.5      # stop at 1.5x ATR below entry
    take_profit_atr: float = 2.0    # partial TP at 2x ATR above entry
    trail_atr: float = 1.0          # trailing stop 1x ATR

    # ── Signal thresholds (autotuner may adjust) ───────────────────────────
    rsi_oversold: float = field(default_factory=lambda: float(os.getenv("RSI_OVERSOLD", "35")))
    rsi_overbought: float = field(default_factory=lambda: float(os.getenv("RSI_OVERBOUGHT", "65")))
    bb_squeeze_threshold: float = field(default_factory=lambda: float(os.getenv("BB_SQUEEZE", "0.03")))
    volume_ratio_threshold: float = field(default_factory=lambda: float(os.getenv("VOL_RATIO", "2.5")))

    # ── Pump detector ──────────────────────────────────────────────────────
    pump_volume_zscore: float = 3.0
    pump_price_change_pct: float = 2.0   # % change in 15min
    pump_scan_interval: int = 60         # seconds

    # ── Listing detector ───────────────────────────────────────────────────
    listing_scan_interval: int = 120     # seconds
    listing_max_hold_min: int = 15       # max 15min for listing plays
    listing_stop_pct: float = 3.0        # tight 3% stop

    # ── Order book ─────────────────────────────────────────────────────────
    ob_depth: int = 20
    ob_imbalance_buy: float = 0.65       # > this = strong buy pressure
    ob_imbalance_sell: float = 0.35      # < this = strong sell pressure
    whale_order_pct: float = 0.005       # single order > 0.5% of 24h vol

    # ── Sentiment ──────────────────────────────────────────────────────────
    sentiment_cache_ttl: int = 900       # 15min

    # ── Autotuner ──────────────────────────────────────────────────────────
    autotuner_day: int = 6               # Sunday (0=Mon, 6=Sun)
    autotuner_hour: int = 3

    # ── Trading pairs — safe pool (large caps, 70% capital) ───────────────
    safe_symbols: tuple = ("BTCUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC")

    # ── Trading pairs — aggressive pool (altcoins, 30% capital) ──────────
    aggr_symbols: tuple = ("SUIUSDC", "NEARUSDC", "DOGEUSDC", "PEPEUSDC")

    @property
    def all_symbols(self) -> tuple:
        return self.safe_symbols + self.aggr_symbols

    @property
    def amr5_ollama_url(self) -> str:
        return f"{self.amr5_host}:{self.amr5_ollama_port}"

    def validate(self) -> list[str]:
        """Return list of warnings for missing/bad config."""
        warnings = []
        if not self.binance_api_key:
            warnings.append("BINANCE_API_KEY not set — cannot trade live")
        if not self.binance_api_secret:
            warnings.append("BINANCE_API_SECRET not set — cannot trade live")
        if not self.telegram_token:
            warnings.append("TELEGRAM_BOT_TOKEN not set — notifications disabled")
        if not self.telegram_chat_id:
            warnings.append("TELEGRAM_CHAT_ID not set — notifications disabled")
        if self.paper_mode:
            warnings.append("PAPER_MODE=true — no real money at risk")
        return warnings


# Singleton
config = Config()
