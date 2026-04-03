# PolyEdge — Complete Codebase Audit Report

**Date:** April 3, 2026
**Auditor:** Claude Opus 4.6
**Codebase:** /Users/adamgrodin/polyedge
**Branch:** main (commit a3d7964)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Total source files** | 103 Python files (src/) |
| **Total lines of code** | 26,582 (src/) + 31,502 (tests/) = 58,084 |
| **Total test files** | 101 test modules |
| **Total test cases** | 2,057 collected |
| **Test-to-code ratio** | 1.19x (excellent) |
| **External API integrations** | 8 (Kalshi REST, Kalshi WebSocket, Anthropic Claude, Serper, DuckDuckGo, FRED, Metaculus, Manifold) |
| **Trading strategies** | 8 active strategies |
| **Environment variables** | 13 total (3 required, 10 optional), all documented |
| **Bare except clauses** | 0 (100% typed) |
| **TODO/FIXME/HACK comments** | 0 |
| **Print statements** | 0 (all via logging) |
| **Dependencies** | 19 pinned in requirements.txt |

---

## 1. Structural Integrity

### Directory Tree

```
polyedge/
├── config/
│   ├── .env                         # Secrets (gitignored)
│   ├── .env.example                 # Template — all 13 vars documented
│   ├── settings.yaml                # Main config (18 sections)
│   ├── categories.yaml              # 13 market categories with weights/keywords
│   └── kalshi_private_key.pem       # RSA key (gitignored)
├── src/                             # 103 Python files
│   ├── main.py                      # Entry point
│   ├── config.py                    # Pydantic config models
│   ├── metrics.py                   # Health metrics tracking
│   ├── core/ (11 files)             # API clients, models, retry
│   ├── analysis/ (22 files)         # AI forecasting, calibration, ensemble
│   ├── strategies/ (9 files)        # 8 trading strategies
│   ├── data/ (17 files)             # Market scanning, enrichment, external data
│   ├── execution/ (7 files)         # Order building, routing, position tracking
│   ├── risk/ (7 files)              # Risk engine, Kelly sizing, circuit breaker
│   ├── storage/ (7 files)           # SQLite with domain-specific mixins
│   ├── alerts/ (4 files)            # iMessage, daily reports
│   ├── dashboard/ (5+templates)     # FastAPI web UI
│   └── orchestrator/ (5 files)      # Main event loops, lifecycle
├── scripts/ (6 standalone)          # Backtest, whale discovery, tax export
├── tests/ (101 files, 14 dirs)      # 2,057 test cases
├── data/                            # Runtime (gitignored)
├── requirements.txt                 # 19 pinned dependencies
├── pyproject.toml                   # Build config, pytest settings
└── ecosystem.config.js              # PM2 deployment config
```

### Source Files by Directory

| Directory | Files | Key Modules |
|-----------|-------|-------------|
| src/core/ | 11 | kalshi_client.py, models.py, websocket_client.py, retry_helper.py |
| src/analysis/ | 22 | claude_forecaster.py, ensemble.py, calibration.py, prompt_templates.py |
| src/strategies/ | 9 | ai_probability.py, cross_arb.py, news_reactive.py, obvious_no.py |
| src/data/ | 17 | market_scanner.py, data_enricher.py, consensus_aggregator.py |
| src/execution/ | 7 | order_builder.py, order_router.py, position_manager.py, fill_tracker.py |
| src/risk/ | 7 | risk_engine.py, kelly_sizer.py, circuit_breaker.py, manipulation_detector.py |
| src/storage/ | 7 | database.py + 6 domain mixins (markets, trades, calibration, risk, stats, whales) |
| src/alerts/ | 4 | alert_manager.py, imessage_alert.py, daily_report.py |
| src/dashboard/ | 5+6 templates | server.py, routes_html.py, routes_api.py, routes_partials.py |
| src/orchestrator/ | 5 | lifecycle.py, startup.py, scan_cycle.py, trade_cycle.py |

### Orphaned Files

**None found.** All 103 source files are imported by orchestrator, strategies, or tests. Optional modules (FastAPI, ChromaDB, scikit-learn, feedparser, websockets) use conditional imports with graceful degradation.

### Dependencies (requirements.txt)

All 19 dependencies are pinned to specific versions:

| Package | Version | Status | Usage |
|---------|---------|--------|-------|
| kalshi-python | 2.1.4 | CORE | Kalshi API client |
| py-clob-client | 0.34.6 | CORE | Polymarket CLOB |
| cryptography | 46.0.5 | CORE | RSA signing |
| anthropic | 0.86.0 | CORE | Claude API |
| httpx | 0.28.1 | CORE | HTTP client (45+ files) |
| pyyaml | 6.0.3 | CORE | Config parsing |
| pydantic | 2.12.5 | CORE | Data validation |
| python-dotenv | 1.2.2 | CORE | .env loading |
| ddgs | 9.11.4 | CORE | DuckDuckGo search |
| fastapi | 0.135.1 | OPTIONAL | Dashboard (conditional import) |
| uvicorn | 0.42.0 | OPTIONAL | ASGI server |
| jinja2 | 3.1.6 | OPTIONAL | HTML templating |
| websockets | 16.0 | OPTIONAL | Real-time prices |
| feedparser | 6.0.12 | OPTIONAL | RSS feeds |
| pytest | 9.0.2 | DEV | Testing |
| pytest-asyncio | 1.3.0 | DEV | Async tests |

**Missing:** numpy (used by `src/risk/monte_carlo.py` but not in requirements.txt).

### PM2 Configuration (ecosystem.config.js)

- Entry: `venv/bin/python -m src.main`
- Auto-restart: true, max 15 restarts
- Min uptime: 10s (prevents restart loops)
- Kill timeout: 60s, restart delay: 10s
- Max memory: 500MB (auto-restart on OOM)
- Logging: `~/.pm2/logs/polyedge-{out,error}.log` with timestamps

---

## 2. Configuration & Environment

### Environment Variables — Complete Inventory

| Env Var | Required | File | Purpose |
|---------|----------|------|---------|
| `KALSHI_API_KEY_ID` | Yes | config.py:365 | Kalshi auth |
| `KALSHI_PRIVATE_KEY_PATH` | Yes | config.py:366 | RSA key path |
| `ANTHROPIC_API_KEY` | Yes | config.py:367 | Claude API |
| `SERPER_API_KEY` | No | config.py:368 | News search |
| `SEARXNG_URL` | No | config.py:369 | Alt search backend |
| `FRED_API_KEY` | No | config.py:370 | Economic data |
| `METACULUS_API_TOKEN` | No | config.py:371 | Community forecasts |
| `POLYMARKET_PRIVATE_KEY` | No | config.py:372 | Polymarket wallet |
| `POLYEDGE_LIVE_ENABLED` | No | config.py:373 | Live trading gate |
| `POLYEDGE_DASHBOARD_KEY` | No | .env.example:30 | Dashboard auth |
| `POLYEDGE_CORS_ORIGINS` | No | .env.example:33 | CORS policy |
| `CONFIRM_NON_US_POLYMARKET` | No | router_polymarket.py:62 | Compliance gate |
| `POLYEDGE_DRY_RUN` | No | config load | Dry run mode |

All documented in `.env.example`. No undocumented env vars. No hardcoded secrets.

### API Endpoint Configurability

All endpoints configurable via settings.yaml:
- Kalshi production/demo hosts with `use_demo` toggle (default: demo)
- Polymarket CLOB/Gamma/Data hosts
- Serper, FRED, Metaculus, Cleveland Fed URLs
- WebSocket endpoint derived from REST host

### Safety Gates for Live Trading

1. **Gate 1**: `trading.mode: "live"` in settings.yaml (default: "paper")
2. **Gate 2**: `POLYEDGE_LIVE_ENABLED=true` env var (default: false)
3. **Gate 3**: First-trade confirmation TTL (1 hour expiry)

---

## 3. Kalshi Integration

### API Endpoints Traced

**Public (no auth):**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/exchange/status` | GET | Health check |
| `/markets` | GET | List markets (paginated) |
| `/markets/{ticker}` | GET | Single market detail |
| `/markets/{ticker}/orderbook` | GET | Orderbook snapshot |
| `/markets/trades` | GET | Trade history |
| `/events` | GET | List events |

**Authenticated:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/portfolio/balance` | GET | Account balance |
| `/portfolio/positions` | GET | Open positions |
| `/portfolio/orders` | GET | Open orders |
| `/portfolio/orders/{id}` | GET | Order status |
| `/portfolio/orders` | POST | Create order |
| `/portfolio/orders/{id}` | DELETE | Cancel order |

### Authentication

- RSA-PSS signature with SHA256 generated per-request (stateless — no token refresh needed)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- Key rotation: `check_key_freshness()` detects file mtime changes, auto-reloads

### Rate Limiting

- Token bucket: 8 requests/second, 10-token burst capacity (configurable)
- 429 handling: Respects `Retry-After` header, exponential backoff (max 10s), 3 retries
- Circuit breaker: 5 consecutive 5xx errors → exponential cooldown (60s → 600s cap)

### Order Placement

- Price: Decimal arithmetic via `dollars_to_cents()` — integer cents (1-99)
- Side mapping: Explicit `Direction → (Side, kalshi_side)` dictionary
- Types: GTC (limit) preferred, FOK (fill-or-kill) for market orders
- Fees: Maker 1.75%, Taker 7% — calculated with Decimal precision
- Validation: price 1-99¢, count > 0, side in (yes/no), market open check

### Position Tracking

- DB persistence: positions table with entry price, quantity, fees
- Crash recovery: loaded from DB on startup, reconciled with Kalshi API in live mode
- Pending orders: tracked in DB to prevent double-counting in exposure calculations
- Stale cleanup: orders >24h automatically pruned

### Settlement

- WebSocket `lifecycle` channel for settlement events
- Binary market validation: settlement must be exactly 0.0 or 1.0
- Exit orders generated for positions in settled markets

### Monetary Calculations

All financial calculations use `Decimal` with `ROUND_HALF_UP`:
- Balance conversion (cents → dollars)
- Fee calculation (maker/taker)
- P&L tracking (entry, exit, proportional fee allocation)
- Position sizing (Kelly → dollar amount → contract count)

---

## 4. AI Forecasting Pipeline

### Claude Integration (claude_forecaster.py, ~950 lines)

**Prompt Engineering — Excellent:**
- System prompt with calibration rules, base rate anchoring, decomposition methodology
- Category-specific templates: Politics, Fed/Macro, Geopolitics, Tech/AI, Culture
- Market price included in every prompt via `{market_price:.0%}` placeholder
- 3-layer prompt injection defense: truncation → pattern matching → character allowlist
- Superforecaster decomposition via `decomposer.py` for compound questions

**Model Parameters:**
- Default: claude-sonnet-4-6 (fast)
- High-stakes (>$50 or >15% edge): claude-opus-4-6
- Category-specific temperatures: Fed 0.20, Politics 0.25, Geopolitics/Tech 0.30, Culture 0.40
- Max tokens: 2000, timeout: 60s

**Response Parsing — 4 fallback strategies:**
1. Direct JSON parse
2. Extract from markdown code blocks
3. Find first `{` / last `}` substring
4. Prose regex extraction with context validation and ambiguity detection
- Fallback: Returns 0.5 with `parse_failed=True`

**Token Tracking & Cost Monitoring:**
- Per-call: input/output tokens, estimated USD cost
- Daily budget: soft limit (configurable, default 1M tokens), hard limit (2x soft)
- Pre-call estimation using rolling average
- Model pricing: Sonnet $3/$15 per M tokens, Opus $15/$60

**Retry Logic:**
- Shared `retry_with_backoff()` helper: max 3 retries, 2-10s exponential backoff
- Retryable: RateLimitError, APIConnectionError
- Non-retryable: AuthenticationError (immediate fail)
- Circuit breaker: 3 consecutive failures → 5-min cooldown

### Ensemble System (ensemble.py)

- **Single-model**: Claude (85%) + Market Price (15%), adjusted by CI width and divergence
- **Multi-model**: Brier-score-weighted averaging when community forecasts available
- Sources: Manifold Markets, Metaculus, market price
- Post-ensemble extremization: 15% log-odds scaling away from 50%
- Market efficiency scoring: 0.3-0.95 based on volume, liquidity, days-to-resolution

### Calibration Tracking

- **Brier score** with 30-day half-life time decay
- **Calibration bins**: 10-bin histogram (predicted vs actual)
- **Per-category accuracy**: category-specific Brier scores
- **Platt scaling**: sigmoid correction learned from historical predictions
- **Gating**: skip categories with Brier >0.30, raise edge for >0.20

### Divergence Gates

Category-specific maximum divergence from market price:
- Data-rich (Politics, Fed): 30%
- Uncertain (Geopolitics): 45%
- Speculative (Culture): 50%
- Extreme price (<15¢ or >85¢): 25%

### Second Forecaster (GPT-4o)

**Not yet implemented.** Architecture supports multi-model via `multi_model_ensemble()` but only Claude is wired. Easy to add via config.

---

## 5. Data Pipeline & News Integration

### Search Pipeline

1. Generate 2-4 targeted queries (cleaned question, + "latest news", entity extraction, abbreviation expansion)
2. DuckDuckGo (primary, free) → Serper (fallback, paid)
3. Aggregate + deduplicate by normalized URL (strips tracking params)
4. Filter stale results (category-specific thresholds: Fed 5d, Politics 14d, Culture 30d)
5. Deduplicate by title word overlap (Jaccard >0.7)
6. Score by relevance (keyword overlap + recency bonus + source trust multiplier)
7. Keep top 5, enrich top 3 with full article text
8. Format into 4000-char context block

### Full Article Fetching (news_fetcher.py)

- HTTP GET with 5s timeout, User-Agent header
- JSON-LD `articleBody` extraction (if available)
- Custom HTML parser skipping nav/footer/script tags
- Quality filter: min 50 words, sentence boundary truncation at 3000 chars

### Serper Resilience

- Auth failure escalation: 1st warning → 2nd 1-hour cooldown → 3rd permanent disable
- Auto-recovery after 1 hour, key rotation resets counter
- Rate limit (429): returns empty, no escalation

### Caching

- Context cache: 500 entries max, 30-min TTL, LRU eviction
- Forecast cache: 5-min TTL per market, invalidated on >5% price move

---

## 6. Trading Logic & Risk Management

### 15-Point Pre-Trade Risk Gate (risk_engine.py)

Every trade must pass ALL checks:

| # | Check | Config | File |
|---|-------|--------|------|
| 1 | Category exclusion | exclude_categories | risk_engine.py:159-192 |
| 2 | Balance check | bankroll - committed | risk_engine.py:194-211 |
| 3 | Position size limit | 5% of bankroll | risk_engine.py:213-222 |
| 4 | Total exposure limit | 40% of bankroll | risk_engine.py:224-235 |
| 5 | Correlated exposure | 20% per event group | risk_engine.py:237-307 |
| 6 | Circuit breaker | halted state | risk_engine.py:309-313 |
| 7 | Liquidity check | <10% of book depth | risk_engine.py:315-331 |
| 8 | Existing position | no double-entry | risk_engine.py:333-351 |
| 9 | Signal quality | confidence ≥60%, edge, probability [0.01-0.99] | risk_engine.py:353-401 |
| 10 | Resolution date | >4 hours to resolution | risk_engine.py:403-418 |
| 11 | Cooldown | 4h after loss, 1h after profit | risk_engine.py:420-434 |
| 12 | Wash trade prevention | 4h between exit/re-entry | risk_engine.py:436-465 |
| 13 | Manipulation detection | rapid moves, slow drift, crossed books | risk_engine.py:467-471 |
| 14 | Obvious NO cap | 10% of bankroll | risk_engine.py:473-486 |
| 15 | Spread vs edge | spread <50% of edge | risk_engine.py:522-539 |

Plus: max 6 concurrent positions (check H-2).

### Position Sizing (kelly_sizer.py, 443 lines)

10-step Kelly criterion with multiple caps:
1. Raw Kelly fraction: `f = (p*b - q) / b`
2. Dynamic Kelly: `f × 0.25 × dynamic_fraction` (scaled by rolling win rate)
3. Dollar amount: `half_kelly × bankroll`
4. Confidence adjustment: `× confidence^1.5` (floor 0.2x)
5. Liquidity adjustment: halve if >10% of book, 75% if >5%
6. Position caps: 5% bankroll (3% for P>0.95)
7. Contract count: `kelly_dollars / cost_price`
8. Fee adjustment: binary search to fit within budget
9. Calibration scaling: `× calibration_multiplier × circuit_breaker_multiplier × regime_multiplier`
10. Minimum 1 contract if edge > 0

### Circuit Breaker (circuit_breaker.py)

| Condition | Trigger | Recovery |
|-----------|---------|----------|
| Daily loss >8% | Immediate halt | 24h wall-clock reset |
| Unrealized loss >15% | Immediate halt | 24h reset |
| Drawdown >20% | Immediate halt | 48h + reduced sizing (if <30%) or manual |
| 3 consecutive losing days | Reduce to 50% sizing | Auto-clear on winning day |
| 5+ consecutive losing days | Full halt | Manual intervention required |
| Daily loss >5% | Reduce sizing 50% | Escalating slowdown |

State persisted to DB for crash recovery. High water mark tracked for drawdown.

### Stop Loss

20% unrealized loss triggers exit. Tightened from 30% with rationale: "Binary markets are rarely worth holding through a 20% drawdown."

### Partial Fill Handling (fill_tracker.py)

- Incremental delta recording (only new fills recorded)
- Crash safety: transactional atomicity (BEGIN/COMMIT)
- Restart recovery: loads partial counts from DB
- Correction bounds: accepts API corrections up to 5% of order size
- Deduplication: `_processed_fills` set prevents double-counting

### P&L Tracking

Full Decimal arithmetic (H-1 fix):
- Weighted average entry price
- Proportional fee allocation on partial exits
- Separate buy_fees vs total_fees tracking
- ROUND_HALF_UP to 4 decimal places

### Manipulation Detection (manipulation_detector.py, 259 lines)

1. **Rapid moves**: >20% relative price change (M-17: relative, not absolute)
2. **Slow drift**: >15% cumulative monotonic move in 30 minutes (4+ snapshots)
3. **Crossed books**: YES+NO deviates >5% from 1.0 (M-6: tightened from 8%)

---

## 7. Backtesting & Performance Tracking

### Backtest Framework (scripts/run_backtest.py)

**Methodology: Signal Replay** — evaluates signal quality using actual resolved outcomes, not re-running Claude (which would introduce lookahead bias).

Metrics: total trades, win rate, total P&L, max drawdown, profit factor, Brier score, Sharpe ratio.

Realistic constraints: actual fees, partial fills, Kelly sizing, circuit breaker triggers.

Acknowledged limitations: no re-running Claude, no market impact simulation, assumes limit fills at mid.

### Decision Logging

Complete audit trail across 4 tables:
- **signals**: strategy, direction, edge, probability, market price, confidence, reasoning, acted_on flag
- **trades**: order_id, price, size, fee, realized_pnl, signal_id linkage
- **calibration_records**: predicted vs actual outcome, timestamps
- **exit_reasons**: stop_loss, time_limit, edge_gone, take_profit, capital_rotation

Risk check failures logged with all 15 failed check details.

### Selection Bias

None detected. Backtest includes all signals (no favorable filtering). Only actual observed outcomes used. Every trade linked to originating signal.

---

## 8. Error Handling & Reliability

### Exception Handling

- ~320 except blocks across codebase — **0 bare excepts**, all typed and specific
- All major handlers include logging
- Hierarchical: specific exceptions caught before generic
- No silently swallowed errors

### Retry Logic

| Integration | Max Retries | Backoff | Rate Limiting | Circuit Breaker |
|-------------|-------------|---------|---------------|-----------------|
| Kalshi REST | 3 | Exponential + jitter | Token bucket 8/s | 5 consecutive 5xx |
| Anthropic | 3 | 2-10s exponential | Semaphore (5 concurrent) | 3 failures → 5min |
| Serper | 2 | 1-30s exponential | 429 → return empty | 3 auth fails → disable |
| DuckDuckGo | 2 | Via retry_helper | N/A | Falls back to Serper |

### Graceful Degradation

- **Claude down**: Falls back to market price ± 0.25 CI
- **Kalshi down**: Circuit breaker halts trading; paper mode unaffected
- **News down**: DuckDuckGo → Serper fallback; auto-probe after 1h cooldown
- **Polymarket down**: Optional, disabled by default; no portfolio impact

### State Persistence on Restart

- Open positions: DB-persisted, loaded on startup, reconciled with Kalshi in live mode
- Pending orders: DB-persisted with 24h stale cleanup
- Circuit breaker: state + high water mark persisted
- Cooldowns: DB-persisted with absolute timestamps
- Signals: logged with acted_on flag (prevents duplicate generation)

### Race Condition Protection

- WebSocket: `_ws_lock` protects connection assignment
- Order router: `_pending_lock` protects pending order state
- Database: `_write_lock` with 30s timeout on all write operations
- PID lock: prevents concurrent pm2 instances

---

## 9. Security Review

### Credential Storage

- All API keys loaded from environment variables only
- `.gitignore` covers: `.env`, `*.pem`, `*.key`, `kalshi_private_key*`, `credentials*.json`
- No secrets found committed to git
- Private key: loaded from file path, file mode 600 enforced
- Log file permissions: 600 (owner read/write only)

### HTTPS

All external APIs use HTTPS. httpx defaults to verify=True (SSL).

### Injection Risks

- No subprocess, os.system, os.popen calls
- No exec() or eval()
- No shell=True
- Prompt injection defense: 3-layer sanitization on all external text fed to Claude

### Sensitive Data Logging

- httpx/urllib3 logging suppressed to WARNING level
- FRED API key masked in logs (`_mask_api_key()`)
- No direct API key logging found

### Database Encryption

Unencrypted SQLite (documented accepted risk). Recommendation: enable FileVault on macOS.

---

## 10. Code Quality

### Large Functions (>50 lines)

| Function | File | Lines | Recommendation |
|----------|------|-------|----------------|
| `run_trading_loop` | lifecycle.py:57-182 | 125 | Extract day-boundary logic |
| `assess_market` | claude_forecaster.py:499-612 | 113 | Extract exception handlers |
| `_call_claude` | claude_forecaster.py:352-426 | 74 | Acceptable complexity |
| `search` | news_researcher.py:176-241 | 65 | Extract Serper state recovery |
| `_build_prompt` | claude_forecaster.py:294-351 | 57 | Acceptable |

### Large Files (>300 lines)

| File | Lines | Status |
|------|-------|--------|
| claude_forecaster.py | ~950 | Could split into api_client + prompt_builder + parser |
| lifecycle.py | ~776 | Organized into functions; consider module extraction |
| risk_engine.py | ~540 | Extract validation checks to validators module |
| scan_cycle.py | ~550 | Organized by strategy; acceptable |

### Code Quality Metrics

- **Bare excepts**: 0
- **Mutable defaults**: 0 (all use `Field(default_factory=...)`)
- **Type hints**: >95% coverage across all modules
- **Print statements**: 0 (all via logging)
- **TODO/FIXME/HACK**: 0
- **Import organization**: consistent (stdlib → third-party → local)
- **Cyclic imports**: 0
- **Copy-paste duplication**: minimal (router implementations are intentionally platform-specific)

---

## 11. Regulatory Compliance

### Platform Usage

- **Kalshi**: Primary platform — CFTC-regulated, legal for US users. All trading logic targets Kalshi.
- **Polymarket**: Secondary, optional, disabled by default (`polymarket.enabled: false`).
  - Residency gate: `CONFIRM_NON_US_POLYMARKET=true` required even for paper trading
  - Legal warning logged on startup if enabled: "Polymarket is not available to US residents"
  - Gate enforced in both live and paper routers

### Position Limits

- Per-position: 5% of bankroll (hard cap)
- Total exposure: 40% (hard cap)
- Max concurrent: 6 positions
- No evidence of attempting to circumvent Kalshi position limits

### Market Manipulation Prevention

- Wash trade prevention: 4h cooldown between exit and re-entry
- Manipulation detector: flags rapid moves, slow drift, crossed books
- No spoofing or layering patterns in order logic (single limit order per signal)

### Record-Keeping

- All trades logged with timestamps, prices, fees, P&L
- Tax report export script: `scripts/export_tax_report.py`
- Signal → trade linkage maintained for full audit trail

---

## 12. Improvement Roadmap Audit

| Feature | Status | Notes |
|---------|--------|-------|
| Kalshi market price in Claude's prompt | ✅ Implemented | `{market_price:.0%}` in every prompt |
| GPT-4o as second forecaster | ❌ Not implemented | Architecture supports it via ensemble.py |
| Superforecaster decomposition | ✅ Implemented | decomposer.py with compound question detection |
| Full article text from search results | ✅ Implemented | news_fetcher.py with HTML parsing |
| Multi-model ensemble with disagreement | ✅ Partially | Claude + market + community forecasts; no second LLM |
| Calibration tracking with Brier scores | ✅ Implemented | Time-decay, per-category, Platt scaling |
| Performance dashboard | ✅ Implemented | FastAPI with HTMX, 6 pages |

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | ✅ RSA-PSS | ✅ All codes | ✅ 3 retries + backoff | ✅ Token bucket 8/s | ✅ Configurable | ✅ 11 files | **Excellent** |
| Kalshi WebSocket | ✅ RSA-PSS | ✅ Reconnect | ✅ Exponential backoff | ✅ Ping/pong | ✅ 30s ping | ✅ Tested | **Good** |
| Anthropic (Claude) | ✅ API key | ✅ Typed errors | ✅ retry_helper | ✅ Semaphore (5) | ✅ 60s | ✅ 17 files | **Excellent** |
| Serper (Search) | ✅ API key | ✅ State machine | ✅ 2 retries | ✅ 429 handling | ✅ 10s | ✅ Tested | **Good** |
| DuckDuckGo | N/A (free) | ✅ Fallback | ✅ 2 retries | N/A | ✅ 10s | ✅ Tested | **Good** |
| FRED | ✅ API key (query) | ✅ Timeout/error | ✅ retry_helper | N/A | ✅ 10s | ✅ Tested | **Good** |
| Metaculus | ✅ API token | ✅ Timeout/error | ✅ retry_helper | N/A | ✅ 10s | ✅ Tested | **Good** |
| Manifold | N/A (public) | ✅ Timeout/error | ✅ retry_helper | N/A | ✅ 10s | ✅ Tested | **Good** |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | ✅ Kalshi + Polymarket scanners | ✅ 11+ tests | ✅ Category exclusion, volume/liquidity filters | **Complete** |
| Forecast Generation | ✅ Claude + ensemble + decomposition | ✅ 17+ tests | ✅ Budget limits, circuit breaker, divergence gates | **Complete** |
| Edge Detection | ✅ Per-strategy thresholds + regime multiplier | ✅ Tested | ✅ Edge decay tracking, minimum edge gates | **Complete** |
| Position Sizing | ✅ Kelly criterion (10-step) | ✅ 7+ tests | ✅ 5% cap, liquidity adj, calibration scaling | **Complete** |
| Order Execution | ✅ Kalshi + Polymarket + Paper routers | ✅ 7+ tests | ✅ Balance preflight, 3-gate live safety | **Complete** |
| Position Tracking | ✅ DB-persisted, crash-safe | ✅ Tested | ✅ Reconciliation, pending order tracking | **Complete** |
| P&L Calculation | ✅ Decimal arithmetic, fee allocation | ✅ Tested | ✅ Proportional fees, ROUND_HALF_UP | **Complete** |
| Settlement Handling | ✅ WebSocket lifecycle + REST fallback | ✅ Tested | ✅ Binary validation, exit generation | **Complete** |

---

## Module-by-Module Scorecard

| Module | Quality | Tests | Error Handling | Risk Controls | Docs | Overall |
|--------|---------|-------|----------------|---------------|------|---------|
| core/kalshi_client.py | 5 | 5 | 5 | 5 | 4 | **5** |
| core/models.py | 5 | 5 | 5 | N/A | 4 | **5** |
| core/websocket_client.py | 4 | 4 | 5 | 4 | 3 | **4** |
| core/retry_helper.py | 5 | 4 | 5 | N/A | 4 | **5** |
| analysis/claude_forecaster.py | 4 | 5 | 5 | 5 | 4 | **4** |
| analysis/ensemble.py | 5 | 5 | 4 | 4 | 4 | **5** |
| analysis/calibration.py | 5 | 5 | 4 | N/A | 4 | **5** |
| analysis/prompt_templates.py | 5 | 4 | 5 | 5 | 4 | **5** |
| analysis/news_researcher.py | 4 | 4 | 5 | 4 | 3 | **4** |
| strategies/ai_probability.py | 5 | 5 | 4 | 5 | 4 | **5** |
| strategies/obvious_no.py | 4 | 4 | 4 | 4 | 4 | **4** |
| strategies/cross_arb.py | 4 | 4 | 4 | 4 | 3 | **4** |
| strategies/news_reactive.py | 4 | 4 | 4 | 4 | 3 | **4** |
| data/market_scanner.py | 5 | 5 | 4 | 4 | 4 | **5** |
| data/data_enricher.py | 4 | 4 | 4 | 3 | 3 | **4** |
| execution/order_builder.py | 5 | 5 | 5 | 5 | 4 | **5** |
| execution/order_router.py | 5 | 5 | 5 | 5 | 4 | **5** |
| execution/position_manager.py | 5 | 5 | 4 | 5 | 4 | **5** |
| execution/fill_tracker.py | 5 | 5 | 5 | 5 | 4 | **5** |
| risk/risk_engine.py | 5 | 5 | 5 | 5 | 4 | **5** |
| risk/kelly_sizer.py | 5 | 5 | 4 | 5 | 4 | **5** |
| risk/circuit_breaker.py | 5 | 5 | 5 | 5 | 4 | **5** |
| risk/manipulation_detector.py | 4 | 4 | 4 | 4 | 3 | **4** |
| storage/database.py | 4 | 4 | 4 | 3 | 4 | **4** |
| orchestrator/lifecycle.py | 4 | 4 | 4 | 4 | 3 | **4** |
| orchestrator/scan_cycle.py | 4 | 4 | 4 | 4 | 3 | **4** |
| dashboard/server.py | 4 | 3 | 4 | 3 | 3 | **3** |
| alerts/alert_manager.py | 4 | 4 | 4 | N/A | 3 | **4** |

**Average: 4.4/5**

---

## Issues by Severity

### 🔴 CRITICAL — Fix Before Next Trade

**No critical issues found.** The codebase has been through multiple audit cycles (H-, M-, L-prefix fixes visible throughout). All monetary calculations use Decimal, all API calls have retry logic, and the 15-point risk gate is comprehensive.

### 🟠 HIGH — Fix This Week

**H-1: numpy missing from requirements.txt**
- **File:** `requirements.txt`
- **What:** `src/risk/monte_carlo.py` imports numpy, but numpy is not listed in requirements.txt
- **Impact:** Fresh install will crash on Monte Carlo simulation. If Monte Carlo is called during risk assessment, position sizing could fall back to defaults or error out.
- **Fix:** Add `numpy>=2.0` to requirements.txt

**H-2: Fill tracker cumulative timeout may abort valid polls**
- **File:** `src/execution/fill_tracker.py:113`
- **What:** 5-minute cumulative timeout for all concurrent fill polls. With multiple pending orders, valid polls may be aborted.
- **Impact:** Missed fills could leave phantom pending orders, inflating exposure calculations and blocking new trades.
- **Fix:** Change to per-order timeout instead of cumulative cap, or increase cumulative limit proportionally to pending order count.

**H-3: Dashboard task not awaited during shutdown**
- **File:** `src/orchestrator/lifecycle.py:569-574`
- **What:** Dashboard asyncio task is not explicitly cancelled/awaited during graceful shutdown sequence.
- **Impact:** Dashboard may continue serving stale data or accepting requests during shutdown, potentially showing inconsistent state.
- **Fix:** Add `dashboard_task.cancel()` and `await dashboard_task` to the shutdown sequence before closing other services.

**H-4: Database write lock timeout of 30s may be exceeded**
- **File:** `src/storage/db_trades.py:118-142`
- **What:** Threading lock with 30s timeout on all write operations. Under heavy signal volume (8 strategies × multiple markets), writes could queue up.
- **Impact:** Lock timeout would cause trade logging to fail silently, breaking the audit trail and potentially missing fill records.
- **Fix:** Increase timeout to 60s, or switch to asyncio-native locking. Add a metric counter for lock timeout events.

**H-5: Missed fills during offline window**
- **File:** `src/execution/fill_tracker.py`
- **What:** If the bot is offline (crash, restart) while WebSocket fill notifications arrive, fills may be missed if the offline window exceeds Kalshi's message buffer.
- **Impact:** Phantom pending orders in DB; exposure calculations inflated; could block legitimate new trades.
- **Fix:** Add a fill reconciliation step on startup that compares DB pending orders against Kalshi's actual order states via REST API.

### 🟡 MEDIUM — Fix When Possible

**M-1: claude_forecaster.py is ~950 lines**
- **File:** `src/analysis/claude_forecaster.py`
- **What:** Single file handles API client, prompt building, response parsing, token tracking, caching, and circuit breaker.
- **Impact:** Maintenance burden. Changes to parsing could accidentally affect token tracking.
- **Fix:** Split into: `claude_api_client.py`, `prompt_builder.py`, `response_parser.py`, `token_tracker.py`.

**M-2: No second LLM forecaster for ensemble**
- **What:** Architecture supports multi-model ensemble but only Claude is wired.
- **Impact:** Single-model risk. If Claude has systematic bias in a category, no cross-check catches it.
- **Fix:** Add GPT-4o integration via OpenAI SDK. Wire into `multi_model_ensemble()`. Can be gated behind `openai_api_key` config.

**M-3: Strategy validation duplication**
- **Files:** `ai_probability.py`, `obvious_no.py`, `news_reactive.py`, etc.
- **What:** Each strategy has similar validation boilerplate.
- **Impact:** Bug fixes to validation must be applied in 8 places.
- **Fix:** Extract common validation to base strategy class or mixin.

**M-4: Risk engine at 540 lines**
- **File:** `src/risk/risk_engine.py`
- **What:** 15 checks in one file, some with complex logic (correlated exposure has 3 fallback implementations).
- **Fix:** Extract check implementations to separate `risk_checks/` module files.

**M-5: _strategy_failures list grows unbounded per cycle**
- **File:** `src/orchestrator/scan_cycle.py`
- **What:** Strategy failure tracking list is never cleared within a cycle.
- **Impact:** Minor memory growth per cycle. Not a leak (cleared between cycles) but could accumulate if a single cycle runs very long.
- **Fix:** Bound the list or convert to a counter.

**M-6: Edge multiplier floor (0.7x) may be too permissive**
- **File:** `src/risk/kelly_sizer.py:421-438`
- **What:** If realized edges are 70% of predicted, the system continues trading at 0.7x sizing.
- **Impact:** Systematic edge overestimation erodes returns.
- **Fix:** Consider raising floor to 0.8x. Track realized vs predicted edge ratio monthly.

**M-7: News strategy edge threshold (3%) may be too low**
- **File:** `src/config.py:74`
- **What:** News-reactive signals with 3% edge compete with faster traders.
- **Impact:** Executed news trades may face adverse selection if edge decays before fill.
- **Fix:** Raise to 4-5% or add time-to-fill check (reject if signal is >60s old).

### 🟢 LOW — Optional

**L-1: Document magic numbers**
- Several undocumented heuristics: dedup similarity 0.7, K-means features 20, Monte Carlo 5000 sims.
- **Fix:** Add brief comments or move to config.

**L-2: httpx SSL verification relies on defaults**
- No explicit `verify=True` in httpx clients.
- **Fix:** Add explicit `verify=True` for defense-in-depth documentation.

**L-3: FRED API key in query parameters**
- Documented accepted risk. Log masking mitigates.
- **Fix:** No action needed unless FRED changes API.

**L-4: Database at-rest encryption**
- SQLite is unencrypted (documented).
- **Fix:** Enable FileVault on macOS (system-level). Consider SQLCipher for defense-in-depth.

**L-5: lifecycle.py at 776 lines**
- Well-organized into functions but large.
- **Fix:** Extract day-boundary logic, background task management into separate modules.

**L-6: Confidence penalty curve may undersize medium-confidence trades**
- `confidence^1.5` at 0.65 → 0.631x (only 3% reduction). May not penalize enough.
- **Fix:** Consider `confidence^2.0` for confidence < 0.7.

---

## Top 10 Recommendations (Prioritized)

### 1. Add numpy to requirements.txt [Risk: breaks fresh install]
`src/risk/monte_carlo.py` imports numpy but it's not in requirements.txt. Fresh `pip install -r requirements.txt` will fail when Monte Carlo is invoked. One-line fix.

### 2. Add fill reconciliation on startup [Risk: phantom orders blocking trades]
If the bot crashes while orders are pending, WebSocket fills may be missed. Add REST-based reconciliation of pending orders vs Kalshi actual order states during startup sequence.

### 3. Fix fill tracker cumulative timeout [Risk: missed fills]
Replace 5-minute cumulative timeout with per-order timeout to prevent valid polls from being aborted when multiple orders are pending.

### 4. Await dashboard task during shutdown [Risk: stale data served]
Add explicit `dashboard_task.cancel()` and `await` to the shutdown sequence to prevent the dashboard from serving inconsistent state.

### 5. Increase database write lock timeout [Risk: lost trade records]
30s may be insufficient under heavy signal volume. Increase to 60s and add a monitoring counter for timeout events.

### 6. Add GPT-4o as second forecaster [Performance: reduce single-model risk]
Architecture already supports it. Wire OpenAI SDK into `multi_model_ensemble()`. Diversifies forecasting risk across models.

### 7. Split claude_forecaster.py into focused modules [Maintainability]
950 lines covering 5 distinct concerns. Split into api_client, prompt_builder, response_parser, token_tracker.

### 8. Raise news strategy edge threshold to 4-5% [Performance: reduce adverse selection]
3% edge on news-reactive trades may be consumed by execution delay. Higher threshold improves signal quality.

### 9. Extract risk engine checks to separate files [Maintainability]
15 checks at 540 lines. Moving check implementations to a `risk_checks/` package improves testability and readability.

### 10. Track and alert on edge decay ratio [Performance: catch systematic overestimation]
If realized edges consistently fall below 80% of predicted, automatically tighten the edge multiplier and alert. Currently floors at 0.7x which may be too lenient.

---

*Audit completed April 3, 2026. Overall assessment: **Production-grade codebase** with comprehensive risk management, robust error handling, and thorough test coverage. No critical issues found. 5 high-priority items warrant attention this week, primarily around edge cases in crash recovery and resource contention.*
