"""Pi Trader — Adaptive Intelligence Bridge.

Tier 1 (always): qwen3.5:0.8b on Pi — 7/10, 10-12s
Tier 2 (AMR5 available): qwen3:8b via Ollama — 8.5/10, 3-5s
Tier 3 (WebSocket active): sub-second entries

LLM is called ONLY for trade validation, not for scanning.
Max 3-8 calls per day to respect Pi RAM + Ollama rate limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from config import config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a crypto trading assistant. Analyze the trading signal and respond with a JSON object only.
No explanation, no markdown, no extra text. Only valid JSON like:
{"action": "buy", "confidence": 0.75, "reason": "Strong momentum with volume surge"}
or
{"action": "skip", "confidence": 0.0, "reason": "Overbought, RSI 78, not enough confirmation"}

action must be exactly "buy" or "skip".
confidence is 0.0-1.0.
reason is max 100 chars."""

_USER_TEMPLATE = """Signal data:
Symbol: {symbol}
Current price: {price}
RSI (15m): {rsi}
EMA trend (15m): {ema_trend}
BB position: {bb_pct}
Volume ratio: {vol_ratio}
Order book imbalance: {ob_imbalance}
Whale bid detected: {whale_bid}
Fear & Greed: {fear_greed} ({fear_greed_label})
Source: {source}
Pump z-score: {pump_zscore}

Evaluate: should we buy now? Risk 2% of portfolio."""


@dataclass
class LLMValidation:
    action: str          # "buy" or "skip"
    confidence: float    # 0.0–1.0
    reason: str
    tier: str            # "local" or "amr5"
    latency_ms: int


class Intelligence:
    def __init__(self):
        self._amr5_available: bool | None = None  # None = unknown
        self._amr5_last_check: float = 0.0
        self._amr5_check_ttl: float = 60.0    # re-check every 60s

    async def _check_amr5(self) -> bool:
        """Check if AMR5 Ollama is reachable. Cached 60s."""
        now = time.time()
        if self._amr5_available is not None and now - self._amr5_last_check < self._amr5_check_ttl:
            return self._amr5_available

        try:
            url = f"{config.amr5_ollama_url}/api/tags"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url, timeout=aiohttp.ClientTimeout(total=config.amr5_timeout)
                ) as r:
                    available = r.status == 200
        except Exception:
            available = False

        self._amr5_available = available
        self._amr5_last_check = now
        logger.debug(f"AMR5 availability: {available}")
        return available

    async def _call_ollama(self, ollama_url: str, model: str, prompt: str, timeout: float) -> str:
        """Call Ollama generate endpoint, return response text."""
        payload = {
            "model": model,
            "prompt": prompt,
            "system": _SYSTEM_PROMPT,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 512,       # small context — fast on Pi
                "num_predict": 100,   # short response
            },
        }
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"{ollama_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                data = await r.json()
                return data.get("response", "")

    def _parse_response(self, text: str) -> dict:
        """Extract JSON from LLM response."""
        text = text.strip()
        # Find first { ... } block
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
        return {"action": "skip", "confidence": 0.0, "reason": "LLM parse error"}

    async def validate(self, signal: dict) -> LLMValidation:
        """
        Validate a trading signal using LLM.
        Tries AMR5 first, falls back to local Pi model.
        """
        prompt = _USER_TEMPLATE.format(
            symbol=signal.get("symbol", "?"),
            price=signal.get("price", 0),
            rsi=f"{signal.get('rsi', 'N/A')}",
            ema_trend=signal.get("ema_trend", "unknown"),
            bb_pct=f"{signal.get('bb_pct', 0.5):.2f}",
            vol_ratio=f"{signal.get('vol_ratio', 1.0):.2f}",
            ob_imbalance=f"{signal.get('ob_imbalance', 0.5):.2f}",
            whale_bid=signal.get("whale_bid", False),
            fear_greed=signal.get("fear_greed", 50),
            fear_greed_label=signal.get("fear_greed_label", "Neutral"),
            source=signal.get("source", "standard"),
            pump_zscore=f"{signal.get('pump_zscore', 0):.1f}",
        )

        # Try AMR5 first
        if await self._check_amr5():
            t0 = time.time()
            try:
                raw = await self._call_ollama(
                    config.amr5_ollama_url,
                    config.amr5_model,
                    prompt,
                    timeout=15.0,
                )
                parsed = self._parse_response(raw)
                latency = int((time.time() - t0) * 1000)
                logger.info(
                    f"AMR5 validation [{latency}ms]: {parsed.get('action')} "
                    f"conf={parsed.get('confidence', 0):.2f}"
                )
                return LLMValidation(
                    action=parsed.get("action", "skip"),
                    confidence=float(parsed.get("confidence", 0.0)),
                    reason=str(parsed.get("reason", ""))[:120],
                    tier="amr5",
                    latency_ms=latency,
                )
            except Exception as e:
                logger.warning(f"AMR5 LLM call failed: {e} — falling back to local")
                self._amr5_available = False  # Mark as unavailable until next check

        # Local Pi model (qwen3.5:0.8b)
        t0 = time.time()
        try:
            raw = await self._call_ollama(
                config.local_ollama_url,
                config.local_model,
                prompt,
                timeout=config.local_llm_timeout,
            )
            parsed = self._parse_response(raw)
            latency = int((time.time() - t0) * 1000)
            logger.info(
                f"Local LLM validation [{latency}ms]: {parsed.get('action')} "
                f"conf={parsed.get('confidence', 0):.2f}"
            )
            return LLMValidation(
                action=parsed.get("action", "skip"),
                confidence=float(parsed.get("confidence", 0.0)),
                reason=str(parsed.get("reason", ""))[:120],
                tier="local",
                latency_ms=latency,
            )
        except Exception as e:
            logger.error(f"Local LLM call failed: {e}")
            # LLM unavailable — return conservative skip
            return LLMValidation(
                action="skip",
                confidence=0.0,
                reason=f"LLM unavailable: {e}",
                tier="local",
                latency_ms=0,
            )

    async def get_tier(self) -> str:
        if await self._check_amr5():
            return "amr5"
        return "local"


# Singleton
intelligence = Intelligence()
