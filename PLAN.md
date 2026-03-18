# Pi Trader — Build Plan

**Target score: 9/10** | Hardware: Raspberry Pi 4 4GB | Runs 24/7 autonomous

---

## Intelligence Tiers

| Tier | Active when | Score | Capabilities |
|------|-------------|-------|-------------|
| 1 | Pi standalone (0.8b local) | 7/10 | Multi-timeframe signals, pump detection, LLM validation |
| 2 | + AMR5 qwen3:8b via HTTP | 8.5/10 | Deep narrative analysis, complex patterns |
| 3 | + WebSocket real-time | 9/10 | Sub-second entries, order book delta streaming |

---

## File Structure

```
pi_trader/
├── PLAN.md                  ← this file
├── config.py                ← all settings from .env
├── db.py                    ← SQLite schema + async helpers
├── indicators.py            ← pure-Python: RSI, BB, MACD, ATR, EMA (no numpy)
├── binance_client.py        ← REST + WebSocket (aiohttp, no SDK)
├── sentiment.py             ← Fear&Greed (free API) + Reddit velocity
├── order_book.py            ← bid/ask imbalance, whale detection
├── pump_detector.py         ← scans 200+ USDC pairs for volume anomalies
├── listing_detector.py      ← Binance RSS → new listings = early pump signal
├── intelligence.py          ← adaptive bridge Pi↔AMR5, LLM validation
├── engine.py                ← core trading logic, position manager
├── notify.py                ← Telegram notifications
├── telegram_bot.py          ← /status /trades /stop /start /mode
├── autotuner.py             ← weekly backtest on last 30d, adjust thresholds
├── main.py                  ← entrypoint, asyncio event loop
├── requirements.txt
├── .env.example
├── setup_pi.sh              ← one-shot install script
└── trading_daemon.service   ← systemd unit
```

---

## Core Architecture

```
main.py
  └─ asyncio gather:
       ├─ engine.py          (main loop, 60s cycle)
       │    ├─ pump_detector  (60s, scans 200+ pairs)
       │    ├─ listing_detector (120s, RSS)
       │    ├─ order_book     (per-symbol, on demand)
       │    ├─ sentiment      (15min cache)
       │    ├─ indicators     (per-symbol OHLCV)
       │    └─ intelligence   (LLM validation, AMR5 bridge)
       ├─ telegram_bot.py    (polling loop)
       └─ autotuner.py       (weekly cron)
```

---

## Trading Logic (engine.py)

### Entry Conditions (need 3/4)
1. Multi-timeframe confluence: RSI + EMA on 5m AND 15m agree
2. Bollinger squeeze breakout OR volume surge > 2.5x average
3. Order book: bid/ask ratio > 1.4 (buyers dominant)
4. Sentiment: Fear&Greed > 45 (not extreme fear)

### Priority Entries (bypass standard filters)
- Pump detector: volume 3x+ on 200+ pair scan → immediate evaluation
- New listing detected: buy within 2 min of listing → 15min momentum play
- Whale order: single bid > 1% of 24h volume → trend follow

### Position Management
- Max concurrent positions: 3
- Per-position risk: 2% of portfolio
- Stop loss: 1.5x ATR below entry (adaptive)
- Take profit: partial 50% at 2x ATR, rest trails
- Trailing stop: monotonic (never moves down), 1.0x ATR trail
- Max hold time: 4h (forced exit if not stopped)

### Capital Split
- 70% safe pool: BTC, ETH, BNB only — standard signals
- 30% aggressive pool: altcoins — pump/listing plays

### Risk Guardrails
- Daily max loss: 5% portfolio → auto-pause 24h
- Consecutive losses: 3 in a row → pause 2h
- No trading 30min before/after weekend close

---

## Indicators (pure Python, no numpy)

All calculated on list[float] via sliding window:
- `ema(prices, period)` — exponential moving average
- `rsi(prices, period=14)` — relative strength index
- `bb(prices, period=20, std=2.0)` — Bollinger Bands (upper, mid, lower, width)
- `macd(prices)` — 12/26/9 MACD + signal + histogram
- `atr(highs, lows, closes, period=14)` — average true range
- `volume_ratio(volumes, period=20)` — current vol / avg vol

---

## Pump Detector

Scans ALL active USDC trading pairs on Binance (200+) every 60s:
1. Fetch 24h ticker stats (single API call, bulk endpoint)
2. Calculate z-score of volume vs rolling 7d average
3. Check price momentum: +3% in last 15min
4. Score: volume_zscore * price_momentum
5. Top candidates → order book check → engine evaluation
6. Threshold: z-score > 3.0 AND price_change_15m > 2%

---

## Listing Detector

1. Poll `https://www.binance.com/en/support/announcement/new-cryptocurrency-listing` (RSS/HTML) every 120s
2. Parse for new USDC pair symbols
3. Cross-check with `/api/v3/exchangeInfo` — confirms listing is live
4. On new listing: notify + immediate engine entry evaluation
5. Hold max 15min, tight stop 3%

---

## Sentiment Layer

- **Fear & Greed Index**: `https://api.alternative.me/fng/` — free, 15min cache
- **CoinGecko trending**: free endpoint, top 7 trending coins → boost score
- **Reddit velocity**: r/CryptoCurrency post rate on symbol — mentions per hour delta
  - Cached 5min, uses Reddit JSON API (no auth)

Score: 0-100. Used as multiplier on signal confidence:
- < 20 (extreme fear): 0.5x multiplier
- 20-45 (fear): 0.8x multiplier
- 45-55 (neutral): 1.0x multiplier
- 55-75 (greed): 1.1x multiplier (momentum favored)
- > 75 (extreme greed): 0.7x multiplier (bubble risk)

---

## Order Book Imbalance

For a given symbol:
1. Fetch top 20 bids + asks
2. `bid_volume = sum(qty for price, qty in bids[:10])`
3. `ask_volume = sum(qty for price, qty in asks[:10])`
4. `imbalance = bid_volume / (bid_volume + ask_volume)` → 0.0–1.0
5. Whale detection: single order > 0.5% of 24h volume
6. Signals:
   - imbalance > 0.65 → strong buy pressure
   - imbalance < 0.35 → strong sell pressure
   - whale bid + imbalance > 0.55 → high-confidence long

---

## Intelligence Bridge (Pi ↔ AMR5)

```python
async def validate_trade(signal: dict) -> dict:
    # Try AMR5 first (if available)
    if await amr5_available():
        return await call_amr5_llm(signal)   # qwen3:8b, deep analysis
    # Fallback: local qwen3.5:0.8b on Pi
    return await call_local_llm(signal)       # 10-12s, lightweight
```

AMR5 availability check: `GET http://192.168.1.23:11435/api/tags` timeout 2s.

LLM prompt: structured JSON in → JSON out (action: buy/skip, confidence: 0-1, reason: str).
LLM called ONLY for trade validation (not scanning). Max 3-5 calls/day.

---

## Autotuner (weekly)

Runs every Sunday at 03:00:
1. Fetch last 30 days OHLCV for all traded symbols from SQLite
2. Backtest current thresholds on historical signals
3. Adjust: RSI oversold threshold, BB squeeze multiplier, volume ratio threshold
4. Log changes to DB, notify Telegram with results
5. Does NOT touch stop-loss or capital split (safety)

---

## Database Schema (SQLite)

```sql
trades       — id, symbol, action, price, qty, pnl, reason, confidence, tier, ts
positions    — symbol, entry_price, qty, stop_loss, take_profit, trail_price, ts
signals      — symbol, score, action, source, data_json, acted_on, ts
performance  — date, total_pnl, win_rate, max_drawdown, trades_count, ts
config       — key, value, updated_at
```

---

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | Portfolio value, open positions, today's PnL |
| `/trades` | Last 10 trades with PnL |
| `/pump` | Latest pump candidates from scanner |
| `/listings` | Recently detected new listings |
| `/stop` | Pause trading (keeps positions open) |
| `/start` | Resume trading |
| `/mode safe` | Switch to safe-only pool |
| `/mode aggr` | Enable aggressive pool |
| `/perf` | Weekly performance summary |

---

## Build Order

1. `config.py` — settings, env vars
2. `db.py` — SQLite, all CRUD helpers
3. `indicators.py` — pure Python math
4. `binance_client.py` — REST + WS
5. `sentiment.py` — Fear&Greed + Reddit
6. `order_book.py` — imbalance + whale
7. `pump_detector.py` — 200+ pair scanner
8. `listing_detector.py` — RSS new listings
9. `intelligence.py` — LLM bridge
10. `engine.py` — main trading logic
11. `notify.py` — Telegram helpers
12. `telegram_bot.py` — bot commands
13. `autotuner.py` — weekly optimizer
14. `main.py` — entrypoint
15. Support files: requirements.txt, .env.example, setup_pi.sh, service

---

## Dependencies (Pi-friendly)

```
aiohttp>=3.9          # async HTTP (no requests)
python-telegram-bot>=20.0  # async bot
feedparser>=6.0       # RSS for listings
```

NO: numpy, pandas, scipy, torch, transformers, sklearn
All math is pure Python lists + statistics module.

---

## Target Metrics

| Metric | Target |
|--------|--------|
| Win rate | > 55% |
| Avg win/loss ratio | > 1.5 |
| Max daily drawdown | < 5% |
| Monthly return | 8-15% |
| Sharpe (estimated) | > 1.5 |
| Signals/day | 5-20 |
| Trades/day | 1-5 |
| LLM calls/day | 3-8 |
| RAM usage | < 400MB (with 0.8b loaded) |
| CPU idle | < 15% |
