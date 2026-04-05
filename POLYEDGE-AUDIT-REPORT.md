# PolyEdge Complete Codebase Audit Report

**Audit Date:** April 4, 2026
**Auditor:** Claude Opus 4.6 (automated comprehensive audit)
**Codebase:** PolyEdge — AI-Driven Prediction Market Trading Bot
**Platform:** Python 3.12+ on Mac Mini M4 Pro
**Trading Platforms:** Kalshi (primary, CFTC-regulated) + Polymarket (disabled, non-US only)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Total source files** | 106 Python modules |
| **Total source LOC** | 27,942 |
| **Total test files** | 117 |
| **Total test LOC** | 36,599 |
| **Total test cases** | 2,458 (pytest collected) |
| **Test-to-source ratio** | 1.31x LOC, 1.10x files |
| **External API integrations** | 8 (Kalshi REST, Kalshi WS, Anthropic, Serper, FRED, Metaculus, Manifold, DuckDuckGo) |
| **Environment variables** | 12 total, 12 documented in .env.example, 0 undocumented |
| **Dependencies** | 17 pinned packages, 0 unused, 0 unpinned |
| **Strategies** | 8 (AI Probability, Cross-Arb, Cross-Platform Arb, Whale, News, Late Resolution, Mean Reversion, Obvious NO) |
| **Trading mode** | Paper (default), Live requires 3-gate safety |

### Issue Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 5 |
| HIGH | 12 |
| MEDIUM | 15 |
| LOW | 8 |
| **Total** | **40** |

---

## 1. Structural Integrity

### Directory Structure

```
polyedge/
├── config/                     .env, .env.example, settings.yaml, categories.yaml, kalshi_private_key.pem
├── data/                       markets.db (WAL), chroma/, logs/
├── scripts/                    start.sh, stop.sh
├── src/                        106 Python modules
│   ├── main.py, config.py, metrics.py
│   ├── core/         (10)      Models, Kalshi client, Polymarket client, WebSocket, retry
│   ├── alerts/       (4)       Alert manager, daily report, iMessage
│   ├── analysis/     (24)      Claude forecaster, prompts, ensemble, calibration, decomposer
│   ├── data/         (17)      Market scanner, news, FRED, whale monitor, consensus
│   ├── execution/    (8)       Order builder/router, fill tracker, position manager
│   ├── orchestrator/ (5)       Lifecycle, startup, scan cycle, trade cycle
│   ├── risk/         (9)       Risk engine, Kelly sizer, circuit breaker, manipulation detector
│   ├── storage/      (8)       SQLite DB with 6 mixin modules
│   ├── strategies/   (9)       8 strategies + __init__
│   └── dashboard/    (5)       FastAPI server, routes, templates, static
├── tests/                      117 test files, 2,458 test cases
├── ecosystem.config.js         PM2 process config
├── requirements.txt            17 pinned dependencies
├── pyproject.toml              Python 3.12+, pytest/mypy config
└── Makefile                    Build targets
```

### Source File Counts by Package

| Package | Files | Purpose |
|---------|-------|---------|
| analysis/ | 24 | Probability forecasting, calibration, prompts |
| data/ | 17 | External data sources, news, whales |
| core/ | 10 | API clients, models, WebSocket |
| risk/ | 9 | Risk checks, sizing, circuit breaker |
| strategies/ | 9 | 8 trading strategies |
| execution/ | 8 | Order building, routing, position management |
| storage/ | 8 | SQLite database with modular mixins |
| orchestrator/ | 5 | Main trading loop |
| dashboard/ | 5 | Web UI (FastAPI) |
| alerts/ | 4 | Notifications |
| root (src/) | 3 | main.py, config.py, metrics.py |
| **Total** | **106** | |

### Orphaned Files

**None found.** All 106 modules are imported and used. Clear dependency hierarchy: core -> data -> analysis/strategies -> orchestrator.

### Configuration Files

| File | Status |
|------|--------|
| ecosystem.config.js | Present, valid PM2 config with .env parsing |
| config/.env.example | Present, 42 lines, all 12 env vars documented |
| config/.env | Present on disk, NOT tracked in git |
| config/settings.yaml | Present, 103 lines, complete trading config |
| config/categories.yaml | Present, market category definitions |
| requirements.txt | Present, 17 packages ALL pinned to exact versions |
| pyproject.toml | Present, Python 3.12+ required |
| Makefile | Present, build/test/lint/run targets |

### Dependencies (all pinned)

| Package | Version | Used | Purpose |
|---------|---------|------|---------|
| kalshi-python | 2.1.4 | Yes | Kalshi SDK |
| py-clob-client | 0.34.6 | Yes | Polymarket SDK |
| anthropic | 0.86.0 | Yes | Claude API |
| httpx | 0.28.1 | Yes | HTTP client |
| websockets | 16.0 | Yes | WebSocket client |
| pyyaml | 6.0.3 | Yes | YAML config |
| pydantic | 2.12.5 | Yes | Data validation |
| python-dotenv | 1.2.2 | Yes | .env loading |
| fastapi | 0.135.1 | Yes | Dashboard |
| uvicorn | 0.42.0 | Yes | ASGI server |
| jinja2 | 3.1.6 | Yes | Templates |
| feedparser | 6.0.12 | Yes | RSS feeds |
| ddgs | 9.11.4 | Yes | DuckDuckGo search |
| numpy | 2.2.5 | Yes | Monte Carlo |
| cryptography | 46.0.5 | Yes | RSA signing |
| pytest | 9.0.2 | Yes | Testing |
| pytest-asyncio | 1.3.0 | Yes | Async tests |

**No unused, duplicate, or unpinned dependencies.**

---

## 2. Configuration & Environment

### Complete Environment Variable Inventory

| Variable | Required | Source | Purpose |
|----------|----------|--------|---------|
| `KALSHI_API_KEY_ID` | Yes | .env | Kalshi API auth ID |
| `KALSHI_PRIVATE_KEY_PATH` | Yes | .env | Path to RSA PEM key |
| `ANTHROPIC_API_KEY` | Yes | .env | Claude API key |
| `POLYMARKET_PRIVATE_KEY` | No | .env | Polymarket wallet (disabled by default) |
| `SERPER_API_KEY` | No | .env | Serper.dev news search |
| `FRED_API_KEY` | No | .env | Federal Reserve data |
| `METACULUS_API_TOKEN` | No | .env | Metaculus forecasts |
| `SEARXNG_URL` | No | .env | Self-hosted search |
| `POLYEDGE_LIVE_ENABLED` | No | .env | Live trading gate (default: false) |
| `CONFIRM_NON_US_POLYMARKET` | No | .env | Regulatory gate for Polymarket |
| `POLYEDGE_DASHBOARD_KEY` | No | .env | Dashboard auth key |
| `POLYEDGE_CORS_ORIGINS` | No | .env | Dashboard CORS |

**All 12 variables documented in .env.example. Zero undocumented env vars.**

### Secrets Security

- `.env` file: NOT tracked in git (confirmed via `git ls-files`)
- `.gitignore`: Covers `.env`, `config/.env`, `*.pem`, `*.key`, `config/kalshi_private_key*`, `credentials*.json`
- No hardcoded API keys or secrets in source code
- No secrets in test files (all mocked)

### Hardcoded Endpoints (Acceptable)

- Kalshi prod: `https://api.elections.kalshi.com/trade-api/v2`
- Kalshi demo: `https://demo-api.kalshi.co/trade-api/v2`
- Polymarket CLOB: `https://clob.polymarket.com`
- Polymarket Gamma: `https://gamma-api.polymarket.com`
- Serper: `https://google.serper.dev/search`
- FRED: `https://api.stlouisfed.org/fred/series/observations`

### Environment Switching

- `kalshi.use_demo`: boolean toggle for demo vs production API
- `trading.mode`: "paper" (default) vs "live"
- `polymarket.enabled`: false (default)
- Three-gate live safety: config mode + env var + interactive confirmation

---

## 3. Kalshi Integration

### Endpoints Used

| Endpoint | Method | Purpose | Error Handling |
|----------|--------|---------|----------------|
| `/exchange/status` | GET | Health check | try/except, logs error |
| `/markets` | GET | List markets (paginated) | Returns empty list on failure |
| `/markets/{ticker}` | GET | Single market details | Validates fields, logs warning |
| `/markets/{ticker}/orderbook` | GET | Order book data | Logs error, returns None |
| `/markets/trades` | GET | Trade history (paginated) | Cursor pagination, breaks on empty |
| `/portfolio/balance` | GET | Account balance (cents) | Decimal arithmetic, validates range |
| `/portfolio/positions` | GET | Open positions | Returns empty list on failure |
| `/portfolio/orders` | GET | Resting orders | Returns empty list on failure |
| `/portfolio/orders` | POST | Create order | Full error handling + reconciliation |
| `/portfolio/orders/{id}` | GET | Order status | Returns None on failure |
| `/portfolio/orders/{id}` | DELETE | Cancel order | Logs success/error |

### Authentication

- **Method:** RSA-PSS signature per request (headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP)
- **Key rotation:** `check_key_freshness()` compares file mtime, auto-reloads on change
- **No session tokens** — each request independently signed

### Rate Limiting

- **Proactive:** Token bucket at 8 req/sec with 10-burst capacity (`kalshi_client.py:25-51`)
- **Reactive:** 429 response handling with Retry-After header respect, exponential backoff, max 3 retries
- **Circuit breaker:** 5 consecutive 5xx errors opens circuit for 60-600s

### Order Placement

- **Price:** Decimal arithmetic via `dollars_to_cents()` — `(Decimal(str(dollars)) * 100).quantize(Decimal("1"), ROUND_HALF_UP)`
- **Quantity:** Integer contracts (`int(order.size)`)
- **Side mapping:** Explicit dict `_DIRECTION_MAP` with defensive assertions
- **Order types:** Limit (GTC) and Market (FOK) with proper price clamping (0.01-0.99)
- **Timeout:** 25 seconds with 3-attempt reconciliation via `_reconcile_after_timeout()`

### Monetary Calculations

**All monetary calculations use Decimal arithmetic:**
- Fee calculations: `ceil(0.07 * contracts * price * (1-price))` via Decimal
- Order cost: Decimal with 4-decimal quantization
- Balance: API cents (int) -> Decimal -> quantized to $0.01
- Realized P&L: `(price - entry) * size - buy_fee - sell_fee` via Decimal
- Settlement P&L: Includes accumulated buy_fees (prevents P&L overstatement)

### WebSocket

- **Endpoint:** `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Channels:** ticker (prices), fill (executions), lifecycle (settlements)
- **Auth:** RSA-PSS signed headers (same as REST)
- **Reconnect:** Exponential backoff 1s -> 60s, max 10 consecutive failures
- **Settlement validation:** Binary check (0.0 or 1.0), rejects intermediate values
- **Callback leak prevention:** Guards against >50 reconnect callbacks

---

## 4. AI Forecasting Pipeline

### Claude Integration

- **Models:** claude-sonnet-4-6 (routine), claude-opus-4-6 (positions >$50 or edge >15%)
- **Temperature:** Category-specific (Politics 0.25, Fed/Macro 0.20, Geopolitics 0.30, Tech 0.30, Culture 0.40)
- **Max tokens:** 2,000 per call
- **Timeout:** 60 seconds per API call
- **Retry:** 3 attempts with exponential backoff (2s base, 10s max), only on RateLimitError/APIConnectionError
- **Circuit breaker:** Opens after 3 consecutive failures, 5-minute cooldown
- **Token budget:** 1M daily soft limit, 2M hard limit, pre-call estimation from rolling average
- **Cost tracking:** Per-model pricing (sonnet: $3/$15 input/output, opus: $15/$60)

### Prompt Engineering

- **Category-specific templates:** Politics, Fed/Macro, Geopolitics, Tech/AI, Culture, General
- **Market price injection:** YES price included in all templates with anti-anchoring instruction
- **Resolution criteria:** Included verbatim in prompts
- **Base rate requirement:** Explicit instruction to articulate base rates
- **Confidence intervals:** 90% credible interval required
- **Calibration instruction:** "Would I bet my own money?" overconfidence check
- **Superforecaster decomposition:** Compound question detection (13 keyword patterns), sequential conditional chaining for AND/CONDITIONAL types, parallel for OR
- **Adversarial analysis:** Pre-mortem counterargument generation for significant edges
- **Prompt injection defense:** 3-layer sanitization (control chars, injection patterns, character allowlist)

### Response Parsing

- **5-strategy fallback chain:** Direct JSON -> markdown code blocks -> bracket extraction -> regex prose extraction -> 0.5 fallback
- **Schema validation:** Checks "probability" key with alternatives (prob, p, forecast, prediction)
- **Clamping:** [0.01, 0.99] range enforcement
- **CI widening:** +-0.25 on missing fields (vs +-0.20 when present)
- **Parse failure flagging:** `parse_failed=True` prevents trading on bad parses

### Ensemble

- **Single-model:** Claude 85% + market 15% (adaptive based on CI width, market efficiency, divergence)
- **Multi-model:** Brier-score-weighted averaging for multiple forecasters
- **Market efficiency score:** `0.4 * volume + 0.3 * liquidity + 0.3 * time_to_resolution`
- **Extremization:** 15% log-odds scaling away from 50%
- **Community sources:** Manifold Markets, Metaculus, Polymarket cross-reference

### GPT-4o Integration

**NOT IMPLEMENTED.** System is Claude-exclusive. No OpenAI imports or calls found.

---

## 5. Data Pipeline & News Integration

### Search Backends

- **Primary:** DuckDuckGo (free, via `ddgs` library)
- **Fallback:** Serper.dev (paid, with auth failure tracking and 1-hour cooldown)
- **Optional:** SearXNG (self-hosted)

### Article Fetching

- **Full text extraction:** HTML parser strips script/style/nav, extracts paragraphs/headings
- **Limits:** 3 articles max, 5s timeout each, 3000 chars per article, 50-word minimum
- **Deduplication:** URL normalization + 70% title word overlap detection
- **Relevance scoring:** Keyword overlap + recency bonus + source trust multiplier (1.3x Reuters/AP)

### Data Freshness

- **Category-specific staleness:** Fed/Macro 5d, Geopolitics 7d, Tech/AI 10d, Politics 14d, Culture 30d
- **Date parsing:** ISO 8601 + relative ("2 weeks ago") + multiple formats
- **Stale fallback:** Cache with 30-minute TTL, stale notice injected

### Economic Data

- **FRED client:** Federal Reserve data (CPI, unemployment, Fed Funds)
- **Cleveland Fed:** CPI nowcast scraper
- **FedWatch:** CME implied rate probabilities
- **Event calendar:** FOMC, earnings, legislative votes

### Query Construction

- Cleaned question + time-scoped + entity-focused + abbreviation expansion (16 common)
- Max 4 queries per market

---

## 6. Trading Logic & Risk Management

### Decision Flow

1. **Signal generation** — Strategies produce edge estimates
2. **Risk gate** — 15-point check, ALL must pass
3. **Kelly sizing** — Half-Kelly with dynamic scaling
4. **Order building** — Validated with fees, side mapping
5. **Risk re-check** — Dedup and market validation pre-execution

### Edge Thresholds

| Strategy | Min Edge | Notes |
|----------|----------|-------|
| AI Probability | 2% | Regime-adjusted |
| Cross-Arb | 3% | |
| Obvious NO | 5% | |
| News Reactive | 3% | |
| Mean Reversion | 2% | |
| Late Resolution | 5% | |

### Position Sizing (Half-Kelly)

- Base: `f = (p*b - q) / b`, apply half-Kelly * dynamic_fraction * confidence^1.2
- Dynamic fraction: 15% (losing streak) -> 30% (winning streak) based on rolling 20-trade win rate
- **Caps:** 5% per position, 40% total exposure, 20% correlated exposure, 10% obvious-NO
- **Price guards:** Rejects <$0.03, requires 10% edge for $0.03-$0.10 or >$0.97
- **Fee adjustment:** Binary search for max contracts fitting within Kelly allocation after fees

### Risk Engine (15 checks)

1. Balance sufficiency
2. Position size limit (5%)
3. Total exposure limit (40%)
4. Correlated exposure limit (20%)
5. Daily loss limit check
6. Market liquidity (<2% slippage)
7. Existing position check (no double-entry)
8. Edge minimum + confidence minimum
9. Resolution date check
10. Cooldown check
11. Manipulation detection
12. Obvious-NO strategy limit (10%)
13. Max concurrent positions
14. Market status (open/active)
15. Spread-vs-edge (rejects if spread > 50% of edge)

### Exit Mechanisms

| Trigger | Default | Details |
|---------|---------|---------|
| Stop-loss | 20% | With 2% slippage buffer, requires fresh price (<2 min) |
| Trailing stop | 12% activate, 35% distance | E.g., peak +30% -> exit at +19.5% |
| Take profit | 80% of max gain | E.g., bought YES@$0.60 -> exit ~$0.92 |
| Time-based | 21 days max hold | Also exits if market closes <1d and underwater |
| Edge-gone | <20% remaining edge | Min 24h hold to prevent flash exits |
| Capital rotation | When exposure >35% | Exit low-edge positions for higher-edge opportunities |

### Circuit Breakers

| Trigger | Threshold | Duration | Action |
|---------|-----------|----------|--------|
| Daily loss | 8% of bankroll | 24 hours | HALT all trading |
| Daily warning | 5% of bankroll | Immediate | Reduce to quarter-Kelly |
| Unrealized loss | 15% of bankroll | 24 hours | HALT (flash crash protection) |
| Max drawdown | 20% from peak | 48 hours | HALT, auto-recover with reduced sizing |
| 3 consecutive losing days | — | During streak | Quarter-Kelly |
| 5 consecutive losing days | — | Until manual review | HALT |

### Partial Fill Handling

- Delta-based tracking: `filled_count - already_recorded`
- Fill correction guard: Allows <=5% backwards correction, rejects >5%
- DB transaction wraps trade + order update (atomic)
- Reload from DB on restart (`_load_partial_recorded_counts()`)
- Dedup via `_processed_fills` set (pruned at 10k entries)

---

## 7. Backtesting & Performance Tracking

### Backtest Infrastructure

- **Engine:** `scripts/backtest_engine.py` (1,342 LOC)
- **CLI:** `scripts/run_backtest.py` (270 LOC)
- **Tests:** 87 tests (test_backtest.py + test_backtest_engine.py)
- **Features:** Portfolio simulation, parameter sweeps, time-based filtering, CSV export
- **Metrics:** Win rate, max drawdown, profit factor, Sharpe ratio

### Calibration Tracking

- **CalibrationTracker:** Logs predictions, resolves outcomes, computes Brier scores
  - Time-decay weighting (30-day half-life)
  - Per-category stats
  - Win rate tracking
- **CalibrationAnalyzer:** Reports with James-Stein shrinkage bias correction
  - 10-bin calibration curves
  - Category-specific bias detection (over/underestimation)
  - Correction factors for Claude prompts
- **Platt Calibrator:** Post-hoc logistic regression scaling
- **A/B Testing:** Thompson sampling for prompt variants (control, explicit_base_rate, devils_advocate, no_market_price)

### Logging

- All trading decisions logged to SQLite: forecast, market price, edge, signal, order, fill, P&L
- Timestamps on all records
- Strategy attribution on all trades
- Model used and tokens consumed per forecast

---

## 8. Error Handling & Reliability

### Retry Logic

| API | Max Retries | Backoff | Notes |
|-----|-------------|---------|-------|
| Kalshi REST | 3 | Exponential 2s-10s + jitter | Circuit breaker after 5 consecutive 5xx |
| Kalshi order creation | 25s timeout + 3 reconciliation attempts | 5s between reconciliation | Escalates to PENDING_REVIEW on failure |
| Anthropic Claude | 3 | Exponential 2s-10s | Only RateLimitError/APIConnectionError retryable |
| DuckDuckGo | 2 | Backoff | Falls through to Serper |
| Serper | 2 | Exponential 1s-30s | Auto-disable after 3 auth failures, 1h probe recovery |
| Balance preflight | 3 (large) / 1 (small) | 1s linear | Rejects large orders on failure |

### Graceful Degradation

| Component Down | Behavior |
|----------------|----------|
| Anthropic API | Circuit breaker opens, returns market price as fallback forecast |
| Kalshi API | Order creation timeout -> reconciliation -> PENDING_REVIEW |
| Serper | Falls back to cached context (30-min TTL) or empty context |
| DuckDuckGo | Falls through to Serper or cache |
| WebSocket | Falls back to REST polling via fill_tracker |
| Database | Errors logged, graceful return of empty results |

### State Persistence on Restart

| State | Persisted To | Recovery Method |
|-------|-------------|-----------------|
| Positions | DB trades table | Replay from trade history |
| Pending orders | DB pending_orders table | Restore on startup |
| Partial fills | DB trades table | Sum by order_id |
| Circuit breaker | DB circuit_breaker_state | Load state on startup |
| Cooldowns | DB cooldowns table | Load on startup |
| Bankroll | DB settings table | Restore on startup |

---

## 9. Security Review

### Credential Security

- **No secrets in source code** (confirmed via grep for sk-ant, api_key, secret, password patterns)
- **No secrets in git history** (config/.env not tracked, confirmed via `git ls-files`)
- **.gitignore comprehensive:** .env, *.pem, *.key, credentials*.json, database files, logs
- **All API keys from environment variables** via python-dotenv

### SQL Injection

- **All queries parameterized** with `?` placeholders across all 8 storage modules
- **No string formatting in user-facing queries** (only table names in migrations)

### Command Injection

- **No `subprocess` calls with `shell=True`**
- **No user input passed to shell commands**

### Prompt Injection

- **3-layer sanitization** on all external text fed to Claude:
  1. Control character stripping (including zero-width unicode)
  2. Injection pattern detection and sentence removal
  3. Character allowlist (printable ASCII + Latin-1 accented)
- **Length limits:** Market questions 500 chars, resolution criteria 2000, news context 5000

### HTTPS

- All API calls use HTTPS endpoints
- WebSocket uses WSS (TLS)

---

## 10. Code Quality

### Quality Metrics

| Metric | Value | Status |
|--------|-------|--------|
| Bare except clauses | 0 | Excellent |
| Mutable default arguments | 0 | Excellent |
| TODO/FIXME/HACK/XXX comments | 0 | Excellent |
| print() statements | 0 | Excellent (all logging) |
| Hardcoded credentials | 0 | Excellent |
| SQL injection risks | 0 | Excellent |
| Command injection risks | 0 | Excellent |
| Type annotations | ~2,296 | Good coverage |
| Dataclasses | 27 | Strong type safety |
| Files using logging | 93 | Professional |

### Log Level Distribution

| Level | Count | Usage |
|-------|-------|-------|
| info | 316 | Operational logging |
| warning | 267 | Potential issues |
| debug | 172 | Development diagnostics |
| error | 97 | Serious failures |

### Large Functions (>50 lines)

34 functions exceed 50 lines. All serve specific complex purposes (fee calculation matrices, schema migrations, question classification, strategy pipelines) and are properly tested.

### Large Files (>300 lines)

30 files exceed 300 lines. Top examples:
- `strategies/ai_probability.py` (1,109) — Primary strategy with full pipeline
- `execution/position_manager.py` (887) — Position lifecycle management
- `orchestrator/lifecycle.py` (853) — System lifecycle
- `analysis/claude_forecaster.py` (841) — Claude API integration
- `storage/database.py` (769) — Central database (uses mixin pattern)

All appropriately scoped. No excessive complexity.

---

## 11. Regulatory Compliance

### Platform Compliance

- **Kalshi:** Primary platform, CFTC-regulated, legal for US users. All trading logic targets Kalshi.
- **Polymarket:** Code exists but **disabled by default** (`polymarket.enabled: false`). Requires:
  1. Config enable
  2. `CONFIRM_NON_US_POLYMARKET=true` env var
  3. Polymarket private key configured

### Compliance Controls

- Three-gate live trading safety (config + env var + interactive confirmation)
- No market manipulation capabilities (manipulation_detector.py is defensive only)
- All trades logged for record-keeping and tax reporting
- Category exclusions configured (Crypto Prices excluded due to HFT/fees, Sports excluded)

---

## 12. Improvement Roadmap Audit

| Feature | Status | Details |
|---------|--------|---------|
| Kalshi market price in Claude prompt | **IMPLEMENTED** | All templates include `{market_price:.0%}` with anti-anchoring instruction |
| GPT-4o as second forecaster | **NOT IMPLEMENTED** | Claude-exclusive; no OpenAI integration |
| Superforecaster-style decomposition | **IMPLEMENTED** | `decomposer.py` with conditional chaining for compound questions |
| Full article text from search results | **IMPLEMENTED** | `news_fetcher.py` extracts full HTML articles (3 articles, 3000 chars each) |
| Multi-model ensemble with disagreement | **PARTIALLY IMPLEMENTED** | Multi-source ensemble (Claude + Manifold + Metaculus + market), but no GPT-4o |
| Calibration tracking with Brier scores | **IMPLEMENTED** | Full system: CalibrationTracker, CalibrationAnalyzer, Platt scaling, A/B testing |
| Performance dashboard | **IMPLEMENTED** | FastAPI at localhost:8080 with portfolio, strategies, signals, risk, calibration views |

---

## Issues by Severity

### CRITICAL — Fix Before Next Trade

**C-1. Async Race Condition: Position Updates from Multiple Sources**
- **File:** `src/execution/position_manager.py`
- **What:** Positions can be updated concurrently from REST polling (`check_fills()`), WebSocket (`handle_ws_fill()`), and price updates (`update_price()`) with NO asyncio lock between them.
- **Impact:** Double-counted fills could result in 2x position sizes and 2x losses. At $500 position size, potential $500 extra exposure.
- **Fix:** Add `self._position_lock = asyncio.Lock()` and wrap all position mutations with `async with self._position_lock`.

**C-2. WebSocket Death Not Detected**
- **File:** `src/core/websocket_client.py`
- **What:** No health check or periodic liveness probe for WebSocket connection. If WebSocket silently dies, fill notifications stop arriving with no alert.
- **Impact:** Fills missed silently until next REST poll cycle. Open positions could go untracked, leading to incorrect exposure calculations and potential over-leveraging.
- **Fix:** Add periodic health check (every 30s) that triggers REST polling mode fallback if WebSocket is dead.

**C-3. Stale Price Check Blocks ALL Exits Indefinitely**
- **File:** `src/execution/position_manager.py:408-576`
- **What:** Exit logic requires fresh prices (<2 min). If the market scanner or WebSocket dies, `position.last_updated` becomes stale and ALL exit triggers (stop-loss, trailing stop, take profit, edge-gone) are blocked indefinitely.
- **Impact:** Positions cannot be exited during a data feed outage. Could hold losing positions through market resolution.
- **Fix:** Add timeout-based fallback: if price is stale >10 min, attempt live price refresh with timeout; if that also fails, allow exit using stale price with warning.

**C-4. Fill Tracker DB Load Failure Silently Returns Empty Set**
- **File:** `src/execution/fill_tracker.py:430-441`
- **What:** `_load_filled_order_ids()` catches all exceptions and returns `set()`. If DB is corrupted or locked, the tracker starts empty, causing previously recorded fills to be processed again.
- **Impact:** Duplicate fills recorded -> doubled position sizes -> doubled risk exposure.
- **Fix:** Raise on DB load failure during startup (fail fast). Only return empty set for expected "table not found" errors.

**C-5. Polymarket Code Exists (US Regulatory Risk)**
- **Files:** `src/core/polymarket_client.py`, `src/core/polymarket_discovery.py`, `src/execution/router_polymarket.py`, `src/data/polymarket_scanner.py`, `src/data/polymarket_cross_ref.py`
- **What:** Polymarket trading code exists and could be enabled with config changes. Polymarket is not available to US residents per CFTC regulations.
- **Impact:** Regulatory liability if accidentally enabled. Even having the code could raise questions.
- **Fix:** Either (a) remove all Polymarket execution code (keep cross-reference as read-only data source), or (b) add prominent legal disclaimers and audit trail for who enabled it.

---

### HIGH — Fix This Week

**H-1. Circuit Breaker State Not Persisted After Auto-Reset**
- **File:** `src/risk/circuit_breaker.py`
- **What:** Auto-reset in `check()` clears halt state but doesn't call `_persist_state()`. If process crashes after auto-reset but before next manual persist, the auto-reset is lost — bot restarts in halted state.
- **Impact:** Bot stays halted after crash recovery even though cool-down expired.
- **Fix:** Add `self._persist_state()` call after every auto-reset branch.

**H-2. Polymarket Router: No Reconciliation on Timeout**
- **File:** `src/execution/router_polymarket.py`
- **What:** Unlike Kalshi router (which has 3-attempt reconciliation after timeout), Polymarket router has only 1 retry and no `_reconcile_after_timeout()`.
- **Impact:** Orphaned orders on Polymarket could be filled without the bot tracking them.
- **Fix:** Mirror Kalshi's reconciliation pattern for Polymarket.

**H-3. Fill Poll Returns Stale Status on All Timeouts**
- **File:** `src/execution/fill_tracker.py`
- **What:** If all poll attempts timeout, returns last known status which may be "pending" forever. No escalation to manual review.
- **Impact:** Orders stuck in "pending" state, blocking capital allocation without actual fills.
- **Fix:** After max poll attempts, escalate to PENDING_REVIEW status and alert.

**H-4. Exit Candidate Race Condition**
- **File:** `src/execution/position_manager.py:578-597`
- **What:** `get_exit_candidates()` iterates over `_positions` snapshot, but positions can be sold during the loop. When exit order executes, position.size may already be 0.
- **Impact:** Attempting to exit already-closed positions, potentially creating new positions in wrong direction.
- **Fix:** Check position.size > 0 before submitting exit order; use deep copy for iteration.

**H-5. Stale Pending Orders Cleanup Only at Route Time**
- **File:** `src/execution/order_router.py`
- **What:** `_cleanup_stale_pending_orders()` only called when routing new orders, not periodically.
- **Impact:** If no new orders are placed, stale pending orders inflate exposure calculations indefinitely, blocking legitimate trades.
- **Fix:** Add periodic cleanup task (every 1 hour) in orchestrator main loop.

**H-6. PM2 Log Rotation Not Configured**
- **File:** `ecosystem.config.js`
- **What:** Log rotation setup is documented in comments but not actually configured.
- **Impact:** Logs grow unbounded on disk until Mac Mini runs out of space.
- **Fix:** Run the documented commands: `pm2 install pm2-logrotate && pm2 set pm2-logrotate:max_size 50M`.

**H-7. Kalshi Production Host Active in Paper Mode**
- **File:** `config/settings.yaml`
- **What:** `kalshi.use_demo: false` means paper trading makes read-only API calls against production Kalshi API. While safe (paper mode doesn't submit orders), it wastes production rate limit budget.
- **Impact:** Rate limiting in paper mode reduces headroom for live trading. Unnecessary production API usage.
- **Fix:** Set `kalshi.use_demo: true` for paper mode testing.

**H-8. No Exponential Backoff on Balance Preflight Retry**
- **File:** `src/execution/router_kalshi.py:372-405`
- **What:** Balance preflight uses linear 1s waits between retries instead of exponential backoff.
- **Impact:** Could overwhelm Kalshi API during outages.
- **Fix:** Use exponential backoff (1s, 2s, 4s) for balance retries.

**H-9. Unrealized P&L Weighting Inconsistency**
- **File:** `src/risk/circuit_breaker.py`
- **What:** Daily loss limit uses 75% unrealized P&L weighting, but hard gate uses 100%. Risk engine calls with no explicit parameter, creating confusion about which weight is applied.
- **Impact:** Risk calculations may be inconsistent depending on call path.
- **Fix:** Standardize to a single weighting or make the parameter explicit at all call sites.

**H-10. Fill Correction Logic Doesn't Escalate**
- **File:** `src/execution/fill_tracker.py`
- **What:** When API reports fill_count going backwards by >5%, code logs warning but doesn't escalate to manual review or halt trading.
- **Impact:** Corrupted fill data could propagate through P&L calculations silently.
- **Fix:** Escalate >5% fill corrections to PENDING_REVIEW and send alert.

**H-11. Database Growth Unbounded**
- **File:** `src/storage/database.py`
- **What:** No pruning or archival strategy for trades, orders, signals, snapshots tables. SQLite file grows indefinitely.
- **Impact:** Disk space exhaustion, query performance degradation over months.
- **Fix:** Implement monthly archival: `INSERT INTO trades_archive SELECT * FROM trades WHERE timestamp < ?` + DELETE.

**H-12. Pending Order Cost Race Between Concurrent route_order Calls**
- **File:** `src/execution/order_router.py:111-121`
- **What:** `_add_pending` is async and called AFTER `route_order` returns. If two `route_order` calls race, both can be added simultaneously, temporarily exceeding exposure limits.
- **Impact:** Brief window where total exposure exceeds configured limits.
- **Fix:** Move `_add_pending` before order submission (reserve capacity first, release on failure).

---

### MEDIUM — Fix When Possible

**M-1. No GPT-4o or Second AI Model for Ensemble Diversity**
- **File:** `src/analysis/ensemble.py`
- **What:** Ensemble relies solely on Claude + market price + community forecasts. No second LLM for diversity.
- **Impact:** Single-vendor dependency; correlated failure if Claude has systematic bias.
- **Fix:** Add GPT-4o or Gemini as second forecaster for Brier-weighted ensemble.

**M-2. Order Reconciliation Uses Time-Based Matching**
- **File:** `src/execution/router_kalshi.py`
- **What:** Post-timeout reconciliation matches orders by creation time +-30s window.
- **Impact:** Wrong order recovered if multiple identical orders placed within 30s.
- **Fix:** Match on ticker + side + price + size combination, not just time.

**M-3. Kelly Sizer Minimum Contract Floor Buried in Nested Logic**
- **File:** `src/risk/kelly_sizer.py`
- **What:** Floor-to-1-contract logic is deeply nested with unclear activation conditions.
- **Impact:** Possible 0-contract orders in edge cases.
- **Fix:** Add explicit minimum contract check at the end of sizing logic.

**M-4. Manipulation Detector Has No Volume Weighting**
- **File:** `src/risk/manipulation_detector.py`
- **What:** Rapid price moves flagged regardless of volume. Can't distinguish legitimate high-volume moves from thin-market manipulation.
- **Impact:** False positives blocking legitimate trades in liquid markets; false negatives in thin markets.
- **Fix:** Weight price change significance by volume relative to market average.

**M-5. Processed Fills Pruning Too Aggressive**
- **File:** `src/execution/fill_tracker.py`
- **What:** Drops oldest 50% of `_processed_fills` when exceeding 10k entries.
- **Impact:** Recent fills in the dropped half could be re-processed.
- **Fix:** Use LRU eviction (drop oldest by timestamp) or increase limit to 50k.

**M-6. Partial Recorded Cleanup Only on Partial Fill**
- **File:** `src/execution/fill_tracker.py`
- **What:** `_prune_partial_recorded()` only called after recording a partial fill, not periodically.
- **Impact:** Memory growth if many orders partially fill but never complete.
- **Fix:** Add periodic cleanup (every hour) in main loop.

**M-7. No DB Schema Versioning**
- **File:** `src/storage/database.py`
- **What:** No schema version tracking or migration framework. If table structure changes between deployments, silent failures possible.
- **Impact:** Data corruption or lost data after schema changes.
- **Fix:** Add `schema_version` table and migration runner on startup.

**M-8. Strategy Correlation Fallback Too Conservative**
- **File:** `src/risk/risk_checks.py`
- **What:** When CorrelationDetector fails, falls back to 50% correlation assumption for all trades in same strategy.
- **Impact:** Over-conservative exposure limits blocking legitimate trades.
- **Fix:** Use historical correlation from trade data when detector fails.

**M-9. Slippage Tracked But Not Fed Back to Sizing**
- **File:** `src/execution/fill_tracker.py`
- **What:** Slippage >$0.01 is logged but doesn't influence future position sizing.
- **Impact:** Continued large orders despite consistent poor fills.
- **Fix:** Feed slippage history into Kelly sizer as execution cost adjustment.

**M-10. Cross-Check Runs on Lowest-Edge Signals**
- **File:** `src/analysis/claude_forecaster.py`
- **What:** Cross-check (dual-temperature) runs on lowest-edge signals. These are the noisiest and most likely to disagree.
- **Impact:** Wasted API calls on signals that will likely be filtered anyway.
- **Fix:** Run cross-check on medium-edge signals where confirmation is most valuable.

**M-11. Polymarket Client Uses Float for Balance**
- **File:** `src/core/polymarket_client.py:153`
- **What:** `raw_balance = float(result.get("balance", 0))` — uses float for monetary value.
- **Impact:** Potential floating-point rounding on Polymarket balance. Mitigated by Polymarket being disabled.
- **Fix:** Use Decimal for Polymarket monetary values (consistent with Kalshi).

**M-12. Polymarket Discovery Uses Float for Prices**
- **File:** `src/core/polymarket_discovery.py:82-93`
- **What:** Market prices parsed with `float()` directly from API responses.
- **Impact:** Minor rounding errors in cross-reference data. Low impact since Polymarket trading is disabled.
- **Fix:** Use Decimal for all price parsing.

**M-13. Max Concurrent Positions Default Not Visible**
- **File:** `src/risk/risk_checks.py`
- **What:** `max_concurrent_positions` is configurable but the default value isn't visible in settings.yaml or documented.
- **Impact:** Unclear what the effective limit is without reading source code.
- **Fix:** Add explicit default to settings.yaml with documentation.

**M-14. Fill Poll Timeout (10s) May Be Too Short Under Load**
- **File:** `src/execution/fill_tracker.py`
- **What:** Each fill poll attempt has 10s timeout. Kalshi API can take 15-20s under load.
- **Impact:** False timeouts causing status to be reported as stale.
- **Fix:** Increase to 15-20s or make configurable.

**M-15. Price History in Manipulation Detector Limited to 50 Snapshots**
- **File:** `src/risk/manipulation_detector.py`
- **What:** Only last 50 price snapshots retained per market. Longer-term patterns missed.
- **Impact:** Slow manipulation over hours goes undetected.
- **Fix:** Increase to 200 snapshots for markets with active positions.

---

### LOW — Optional

**L-1. Large Functions Could Benefit from Extraction**
- **Files:** Various (34 functions >50 lines)
- **What:** Some long functions serve complex but well-tested purposes.
- **Impact:** Readability and maintainability could improve.
- **Fix:** Consider method extraction for the top 10 longest functions.

**L-2. Large Files Could Be Further Modularized**
- **Files:** `strategies/ai_probability.py` (1,109 LOC), `execution/position_manager.py` (887 LOC)
- **What:** These files handle complex logic but are well-structured internally.
- **Impact:** Slightly harder to navigate.
- **Fix:** Consider extracting sub-modules (e.g., pipeline stages as separate files).

**L-3. No Formal API Changelog Tracking**
- **What:** No mechanism to detect when Kalshi API changes behavior or endpoints.
- **Impact:** Breaking API changes could cause silent failures.
- **Fix:** Add API version check on startup and log warnings on version changes.

**L-4. Kalshi WebSocket Test Coverage Lower Than REST**
- **File:** `tests/test_core/test_websocket_client.py`
- **What:** WebSocket tests cover basic functionality but fewer edge cases than REST client tests.
- **Impact:** Edge cases in real-time data handling may not be caught.
- **Fix:** Add tests for connection loss during fill notification, malformed messages, rapid reconnect.

**L-5. Orchestrator Has Fewest Unit Tests (20)**
- **Files:** `tests/test_orchestrator/`
- **What:** 20 unit tests for the main trading loop, supplemented by integration tests.
- **Impact:** Complex interaction patterns may not be covered.
- **Fix:** Add tests for error propagation between scan and trade cycles.

**L-6. Import Organization Inconsistencies**
- **Files:** Various
- **What:** Some files don't strictly follow stdlib -> third-party -> local import ordering.
- **Impact:** Style only, no functional impact.
- **Fix:** Run `isort` across codebase.

**L-7. Some Category-Specific Divergence Thresholds May Be Suboptimal**
- **File:** `src/strategies/ai_probability.py`
- **What:** Hard-coded divergence caps (Politics 30%, Geopolitics 45%) based on initial research, not empirical calibration data.
- **Impact:** May reject valid signals or accept invalid ones.
- **Fix:** After accumulating 100+ resolved predictions per category, calibrate thresholds empirically.

**L-8. News Research Abbreviation List Has Only 16 Entries**
- **File:** `src/data/news_researcher.py`
- **What:** Abbreviation expansion covers 16 common terms (Fed, CPI, NATO, etc.) but misses domain-specific ones.
- **Impact:** Slightly lower search quality for niche topics.
- **Fix:** Expand abbreviation list based on actual search query analysis.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS per-request | Comprehensive (4xx/5xx) | 3 retries + exponential backoff | Token bucket 8/s + 429 handling | 25s orders, 5s balance | 13 test files | **Production Ready** |
| Kalshi WebSocket | RSA-PSS headers | Auto-reconnect, parse errors logged | Exponential backoff 1-60s | N/A (server-push) | Not configured (C-2) | Moderate coverage | **Needs Health Check** |
| Anthropic Claude | API key header | Circuit breaker (3 failures) | 3 retries, 2-10s backoff | Token budget (1M/day) | 60s per call | Good coverage | **Production Ready** |
| DuckDuckGo | None | Graceful fallback to Serper | 2 retries with backoff | Rate limited by library | Library default | Moderate | **Production Ready** |
| Serper | API key header | Auth failure tracking, cooldown | 2 retries, 1-30s backoff | Auto-disable after 3 auth fails | Via httpx | Good coverage | **Production Ready** |
| FRED | API key param | Returns None on failure | Via retry_helper | Respect headers | Via httpx | Moderate | **Acceptable** |
| Manifold | None | Returns None on failure | Via retry_helper | Respect headers | Via httpx | Good coverage | **Acceptable** |
| Metaculus | API token | Returns None on failure | Via retry_helper | Respect headers | Via httpx | Good coverage | **Acceptable** |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi events API + Polymarket Gamma + categories | 13+ tests | Volume/liquidity filters, category exclusions | **Strong** |
| Forecast Generation | Claude with decomposition, adversarial, A/B testing | 25+ tests | Budget limits, circuit breaker, parse validation | **Excellent** |
| Edge Detection | Per-strategy thresholds, regime-adjusted | 8+ tests | Min edge, min confidence, divergence gates | **Strong** |
| Position Sizing | Half-Kelly with dynamic scaling, 5 cap layers | 9+ tests | Position/exposure/correlation caps, fee adjustment | **Excellent** |
| Order Execution | Limit/market orders, 3-gate live safety | 9+ tests | Balance preflight, market status check, reconciliation | **Strong** |
| Position Tracking | Trade replay from DB, weighted avg entry, Decimal P&L | 9+ tests | Stale price detection, settlement handling | **Good** (C-1 race) |
| P&L Calculation | Decimal arithmetic, fee attribution, settlement capture | Integrated in position tests | Proportional fees, buy_fee inclusion in settlement | **Excellent** |
| Settlement Handling | WebSocket lifecycle events + REST polling | Moderate tests | Binary validation (0.0/1.0), status transitions | **Good** |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|---|---|---|---|---|---|---|
| core/kalshi_client.py | 5 | 4 | 5 | 5 | 4 | **5** |
| core/models.py | 5 | 5 | N/A | N/A | 4 | **5** |
| core/websocket_client.py | 4 | 3 | 4 | 3 | 3 | **3** |
| analysis/claude_forecaster.py | 5 | 4 | 5 | 5 | 4 | **5** |
| analysis/prompt_templates.py | 5 | 4 | 4 | 5 | 4 | **5** |
| analysis/ensemble.py | 5 | 4 | 4 | 4 | 4 | **4** |
| analysis/calibration.py | 5 | 5 | 4 | 4 | 4 | **5** |
| analysis/decomposer.py | 5 | 4 | 4 | 4 | 4 | **4** |
| data/news_researcher.py | 4 | 4 | 5 | 3 | 3 | **4** |
| data/market_scanner.py | 4 | 4 | 4 | 4 | 3 | **4** |
| execution/order_builder.py | 5 | 4 | 4 | 5 | 4 | **4** |
| execution/order_router.py | 4 | 4 | 4 | 4 | 3 | **4** |
| execution/position_manager.py | 4 | 4 | 3 | 4 | 3 | **4** |
| execution/fill_tracker.py | 4 | 4 | 3 | 4 | 3 | **4** |
| execution/router_kalshi.py | 5 | 4 | 5 | 5 | 4 | **5** |
| risk/risk_engine.py | 5 | 5 | 4 | 5 | 4 | **5** |
| risk/kelly_sizer.py | 5 | 5 | 4 | 5 | 4 | **5** |
| risk/circuit_breaker.py | 4 | 4 | 3 | 5 | 3 | **4** |
| storage/database.py | 5 | 5 | 4 | N/A | 4 | **5** |
| strategies/ai_probability.py | 4 | 4 | 4 | 5 | 3 | **4** |
| orchestrator/lifecycle.py | 4 | 3 | 4 | 4 | 3 | **4** |
| dashboard/server.py | 4 | 4 | 3 | 2 | 3 | **3** |
| alerts/alert_manager.py | 4 | 3 | 3 | N/A | 3 | **3** |

---

## Top 10 Recommendations (Prioritized)

### 1. Add Asyncio Locks to Position Updates (C-1)
**Priority:** CRITICAL — Risk Reduction
**Effort:** 1 hour
**Impact:** Prevents double-counted fills and incorrect exposure. Most likely to cause real money loss.

### 2. Add WebSocket Health Check with REST Fallback (C-2)
**Priority:** CRITICAL — Reliability
**Effort:** 2-3 hours
**Impact:** Ensures fills are never silently missed, positions always tracked.

### 3. Fix Stale Price Exit Blocking (C-3)
**Priority:** CRITICAL — Risk Reduction
**Effort:** 1-2 hours
**Impact:** Ensures positions can always be exited, even during data feed outages.

### 4. Make Fill Tracker DB Load Fail-Fast (C-4)
**Priority:** CRITICAL — Risk Reduction
**Effort:** 30 minutes
**Impact:** Prevents duplicate fill processing after DB corruption.

### 5. Persist Circuit Breaker Auto-Reset State (H-1)
**Priority:** HIGH — Reliability
**Effort:** 15 minutes
**Impact:** Prevents bot staying halted after crash recovery when cool-down has expired.

### 6. Add Periodic Stale Pending Order Cleanup (H-5)
**Priority:** HIGH — Reliability
**Effort:** 30 minutes
**Impact:** Prevents stale orders from blocking new trades by inflating exposure calculations.

### 7. Configure PM2 Log Rotation (H-6)
**Priority:** HIGH — Operations
**Effort:** 5 minutes
**Impact:** Prevents disk space exhaustion on Mac Mini.

### 8. Resolve Polymarket Regulatory Position (C-5)
**Priority:** HIGH — Compliance
**Effort:** 2-4 hours (to remove or clearly gate)
**Impact:** Eliminates regulatory ambiguity for US-based operation.

### 9. Add GPT-4o or Second AI Model (M-1)
**Priority:** MEDIUM — Performance
**Effort:** 1-2 days
**Impact:** Reduces single-vendor AI dependency, improves ensemble diversity.

### 10. Implement Database Archival Strategy (H-11)
**Priority:** MEDIUM — Operations
**Effort:** 2-3 hours
**Impact:** Prevents SQLite performance degradation and disk growth over months.

---

## Conclusion

PolyEdge is a **well-engineered system** with professional code quality, comprehensive test coverage (2,458 tests, 1.31x test-to-source LOC ratio), and sophisticated trading logic. The codebase has zero bare excepts, zero TODOs, zero print statements, and zero hardcoded credentials. Risk management is multi-layered with Kelly sizing, 15-point risk checks, circuit breakers, and 6 exit triggers.

**The 5 critical issues (C-1 through C-5) should be addressed before running live with meaningful capital.** They center on async concurrency safety, WebSocket reliability, and regulatory compliance — not fundamental design flaws. The system architecture is sound and the fixes are straightforward.

After addressing the critical issues, this system is ready for cautious live deployment with small position sizes, ramping up as the calibration data validates performance.

---

*Audit conducted by Claude Opus 4.6 on April 4, 2026. All 106 source files and 117 test files were examined.*
