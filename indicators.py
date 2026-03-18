"""Pi Trader — Technical Indicators.

Pure Python, no numpy/pandas. Operates on list[float].
All functions return None if not enough data.
"""

from __future__ import annotations

import math
import statistics
from typing import NamedTuple


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(prices: list[float], period: int) -> list[float]:
    """Exponential moving average. Returns same length as input (leading values = SMA seed)."""
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    # Seed with SMA of first `period` values
    seed = sum(prices[:period]) / period
    result.append(seed)
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def ema_last(prices: list[float], period: int) -> float | None:
    """Last EMA value."""
    vals = ema(prices, period)
    return vals[-1] if vals else None


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(prices: list[float], period: int = 14) -> float | None:
    """RSI of the last candle. Returns 0–100 or None if not enough data."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


# ── Bollinger Bands ───────────────────────────────────────────────────────────

class BB(NamedTuple):
    upper: float
    mid: float
    lower: float
    width: float      # (upper - lower) / mid
    pct_b: float      # (price - lower) / (upper - lower), 0=at lower, 1=at upper


def bollinger(prices: list[float], period: int = 20, std_mult: float = 2.0) -> BB | None:
    if len(prices) < period:
        return None
    window = prices[-period:]
    mid = sum(window) / period
    variance = sum((p - mid) ** 2 for p in window) / period
    std = math.sqrt(variance)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid if mid else 0.0
    last = prices[-1]
    band_range = upper - lower
    pct_b = (last - lower) / band_range if band_range else 0.5
    return BB(upper=upper, mid=mid, lower=lower, width=width, pct_b=pct_b)


def bb_squeeze(prices: list[float], period: int = 20, threshold: float = 0.03) -> bool:
    """True when bands are narrow (squeeze = potential breakout)."""
    bb = bollinger(prices, period)
    return bb is not None and bb.width < threshold


# ── MACD ──────────────────────────────────────────────────────────────────────

class MACD(NamedTuple):
    macd: float       # fast EMA - slow EMA
    signal: float     # EMA(9) of macd
    histogram: float  # macd - signal


def macd(prices: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> MACD | None:
    if len(prices) < slow + signal_period:
        return None
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    # Align lengths (slow_ema is shorter by slow-fast values)
    diff = len(fast_ema) - len(slow_ema)
    fast_aligned = fast_ema[diff:]
    macd_line = [f - s for f, s in zip(fast_aligned, slow_ema)]
    if len(macd_line) < signal_period:
        return None
    sig_line = ema(macd_line, signal_period)
    if not sig_line:
        return None
    m = macd_line[-1]
    s = sig_line[-1]
    return MACD(macd=m, signal=s, histogram=m - s)


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    # Wilder's smoothing
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


# ── Volume ratio ──────────────────────────────────────────────────────────────

def volume_ratio(volumes: list[float], period: int = 20) -> float | None:
    """Current volume / average of last `period` volumes. >2.5 = surge."""
    if len(volumes) < period + 1:
        return None
    avg = sum(volumes[-period - 1:-1]) / period
    return volumes[-1] / avg if avg > 0 else None


# ── Multi-timeframe signal ────────────────────────────────────────────────────

class TFSignal(NamedTuple):
    bullish: bool
    bearish: bool
    rsi_val: float | None
    ema_trend: str    # "up", "down", "flat"
    bb_pct: float | None
    vol_ratio: float | None


def timeframe_signal(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 65.0,
) -> TFSignal:
    rsi_v = rsi(closes)
    ema_fast = ema_last(closes, 9)
    ema_slow = ema_last(closes, 21)
    bb_v = bollinger(closes)
    vol_r = volume_ratio(volumes)

    # EMA trend
    if ema_fast is not None and ema_slow is not None:
        if ema_fast > ema_slow * 1.001:
            ema_trend = "up"
        elif ema_fast < ema_slow * 0.999:
            ema_trend = "down"
        else:
            ema_trend = "flat"
    else:
        ema_trend = "flat"

    bb_pct = bb_v.pct_b if bb_v else None

    bullish = (
        (rsi_v is not None and rsi_v < rsi_oversold)
        or (ema_trend == "up" and bb_pct is not None and bb_pct > 0.5)
        or (vol_r is not None and vol_r > 2.5 and ema_trend != "down")
    )
    bearish = (
        (rsi_v is not None and rsi_v > rsi_overbought)
        or (ema_trend == "down" and bb_pct is not None and bb_pct < 0.5)
    )

    return TFSignal(
        bullish=bullish,
        bearish=bearish,
        rsi_val=rsi_v,
        ema_trend=ema_trend,
        bb_pct=bb_pct,
        vol_ratio=vol_r,
    )


# ── Multi-timeframe confluence ────────────────────────────────────────────────

class MTFSignal:
    """Multi-timeframe confluence result."""
    def __init__(
        self,
        score: float,          # -100 to +100 (positive = bullish)
        direction: str,        # "long", "short", "neutral"
        agreement: int,        # how many TFs agree (0-4)
        details: dict,         # per-TF breakdown
    ):
        self.score = score
        self.direction = direction
        self.agreement = agreement
        self.details = details

    def __repr__(self) -> str:
        return f"MTF(score={self.score:.1f}, dir={self.direction}, agree={self.agreement}/4)"


def multi_timeframe_confluence(
    candles_5m: list[dict],
    candles_1h: list[dict],
    candles_4h: list[dict],
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 65.0,
) -> MTFSignal:
    """
    Combine 5m, 1h, 4h signals into a confluence score.

    candles_* are lists of {"open","high","low","close","volume"} dicts.
    Returns MTFSignal with score -100..+100 and direction.

    Strategy: higher timeframes have more weight.
    4h = 40%, 1h = 35%, 5m = 25%
    Entry only when at least 2/3 timeframes agree on direction.
    """
    weights = {"5m": 0.25, "1h": 0.35, "4h": 0.40}
    details: dict[str, dict] = {}

    def _tf_score(candles: list[dict], label: str) -> float:
        if len(candles) < 30:
            return 0.0
        closes  = [c["close"]  for c in candles]
        highs   = [c["high"]   for c in candles]
        lows    = [c["low"]    for c in candles]
        volumes = [c["volume"] for c in candles]

        sig = timeframe_signal(closes, highs, lows, volumes, rsi_oversold, rsi_overbought)
        bb  = bollinger(closes)
        mac = macd(closes)

        s = 0.0

        # RSI contribution
        if sig.rsi_val is not None:
            if sig.rsi_val < rsi_oversold:
                s += 25 * (1 - sig.rsi_val / rsi_oversold)
            elif sig.rsi_val > rsi_overbought:
                s -= 25 * ((sig.rsi_val - rsi_overbought) / (100 - rsi_overbought))

        # EMA trend
        if sig.ema_trend == "up":
            s += 20
        elif sig.ema_trend == "down":
            s -= 20

        # Bollinger — price near lower band = bullish setup
        if bb is not None:
            if bb.pct_b < 0.2:
                s += 15
            elif bb.pct_b > 0.8:
                s -= 15
            if bb.width < 0.03:   # squeeze = imminent breakout
                s += 8

        # MACD histogram crossing zero
        if mac is not None:
            if mac.histogram > 0 and mac.macd > mac.signal:
                s += 15
            elif mac.histogram < 0 and mac.macd < mac.signal:
                s -= 15

        # Volume
        vr = volume_ratio(volumes)
        if vr is not None:
            if vr > 2.0:
                s += 10 * min(vr / 3.0, 1.0)
            elif vr < 0.5:
                s -= 5

        details[label] = {
            "score": round(s, 1),
            "rsi": round(sig.rsi_val, 1) if sig.rsi_val else None,
            "ema": sig.ema_trend,
            "bb_pct": round(bb.pct_b, 2) if bb else None,
            "macd_hist": round(mac.histogram, 6) if mac else None,
            "vol_ratio": round(vr, 2) if vr else None,
        }
        return s

    s_5m = _tf_score(candles_5m, "5m")
    s_1h = _tf_score(candles_1h, "1h")
    s_4h = _tf_score(candles_4h, "4h")

    weighted = (
        s_5m  * weights["5m"]
        + s_1h * weights["1h"]
        + s_4h * weights["4h"]
    )

    # Count agreement
    bullish = sum(1 for s in (s_5m, s_1h, s_4h) if s > 10)
    bearish = sum(1 for s in (s_5m, s_1h, s_4h) if s < -10)
    agreement = max(bullish, bearish)

    if bullish >= 2 and weighted > 10:
        direction = "long"
    elif bearish >= 2 and weighted < -10:
        direction = "short"
    else:
        direction = "neutral"

    return MTFSignal(
        score=round(weighted, 1),
        direction=direction,
        agreement=agreement,
        details=details,
    )



# ── SMA ───────────────────────────────────────────────────────────────────────

def sma(prices: list[float], period: int) -> list[float]:
    """Simple moving average list."""
    if len(prices) < period:
        return []
    return [sum(prices[i:i + period]) / period for i in range(len(prices) - period + 1)]


def sma_last(prices: list[float], period: int) -> float | None:
    """Last SMA value."""
    vals = sma(prices, period)
    return vals[-1] if vals else None


# ── ROC (Rate of Change) ──────────────────────────────────────────────────────

def roc(prices: list[float], period: int = 10) -> float | None:
    """Rate of Change: % change vs `period` bars ago.
    Positive = upward momentum, negative = downward momentum.
    """
    if len(prices) < period + 1:
        return None
    past = prices[-(period + 1)]
    if past == 0:
        return None
    return (prices[-1] - past) / past * 100


# ── Z-score ───────────────────────────────────────────────────────────────────

def zscore(values: list[float]) -> float | None:
    """Z-score of last value vs the series. Used in pump detector."""
    if len(values) < 3:
        return None
    try:
        mean = statistics.mean(values)
        std = statistics.stdev(values)
        if std == 0:
            return 0.0
        return (values[-1] - mean) / std
    except statistics.StatisticsError:
        return None
