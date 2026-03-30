# PolyEdge Codebase Audit Report

**Date:** March 30, 2026
**Auditor:** Claude Opus 4.6 (Automated)
**Scope:** Complete 12-section audit per POLYEDGE-AUDIT-PROMPT.md
**Codebase:** PolyEdge AI Trading Bot (Kalshi + Polymarket)
**Codebase Revision:** `1d27388` (Audit revision 21)
**Runtime:** Python 3.12+ on Mac Mini M4 Pro via pm2

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Total source files** | 66 (.py in src/ + scripts/) |
| **Total test files** | 66 (.py in tests/) |
| **Source lines of code** | 17,901 (src/) |
| **Test lines of code** | 13,883 (tests/) |
| **Script lines of code** | 1,611 (scripts/) |
| **Total lines of code** | 33,395 |
| **Test functions** | 933 |
| **Tests passing** | 930 passed, 3 skipped |
| **Test pass rate** | 100% (of non-skipped) |
| **External API integrations** | 8 (Kalshi REST, Kalshi WS, Anthropic, DuckDuckGo, Serper, FRED, Manifold, Polymarket) |
| **Env var count** | 12 total (10 documented, 2 undocumented) |
| **Pinned dependencies** | 16/16 (100%) |
| **Unused dependencies** | 0 |

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS signing | HTTPStatusError, RequestError, JSON parse | 3x exponential backoff (2-10s) | Retry-After header + backoff | 30s hard timeout | Mocked | **STRONG** |
| Kalshi WebSocket | RSA auth headers | Auto-reconnect (1-60s backoff) | 10 consecutive failure limit | N/A | Ping 20s / Pong 30s | Mocked | **STRONG** |
| Anthropic (Claude) | API key (env var) | RateLimitError, ConnectionError, Timeout | 3x exponential (2-10s) | Budget soft/hard limits | 60s configurable | Mocked | **STRONG** |
| DuckDuckGo (News) | None | Fallback to Serper | 2x retry | N/A | 5s per article | Mocked | **GOOD** |
| Serper (Search) | API key (env var) | Auth failure -> permanent disable | 2x exponential | 1h cooldown on failure | Default httpx | Mocked | **GOOD** |
| FRED (Economic) | API key (env var) | Returns empty on failure | 1x retry | N/A | 5s | Mocked | **GOOD** |
| Manifold Markets | None | Returns empty on failure | No retry | N/A | 4s | Mocked | **GOOD** |
| Polymarket CLOB | Wallet signing | HTTPStatusError | 3x with backoff | Semaphore(5) | 30s | Mocked | **GOOD** |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi events API + Polymarket Gamma | Category/volume/liquidity filters | Excluded categories enforced at scan | **STRONG** |
| Forecast Generation | Claude Sonnet/Opus, category-specific prompts | 4-strategy JSON fallback parsing | Circuit breaker, budget limits | **STRONG** |
| Edge Detection | Ensemble (Claude + market + community) | Calibration-adjusted, cross-checked | Divergence gates, CI width gates | **STRONG** |
| Position Sizing | Half-Kelly with 4-layer caps | Fee-accurate binary search | 5% per position, 40% total, 20% correlated | **STRONG** |
| Order Execution | Paper + live modes, maker preferred | 3-gate safety for live trades | Balance, liquidity, manipulation checks | **STRONG** |
| Position Tracking | DB-persisted, API-reconciled | Crash-resistant partial fill tracking | Sync with Kalshi on reconnect | **GOOD** |
| P&L Calculation | Fee-inclusive, proportional allocation | Realized + unrealized tracking | Settlement P&L via synthetic trades | **GOOD** |
| Settlement Handling | WebSocket lifecycle + REST sync | Auto-detect on reconnect | Synthetic close trade logged | **GOOD** |

---

## Section 1: Structural Integrity

### Directory Tree

```
polyedge/
├── config/
│   ├── .env                    (gitignored)
│   ├── .env.example
│   ├── settings.yaml
│   └── categories.yaml
├── scripts/                    (5 files, 1,611 LOC)
│   ├── backfill_markets.py
│   ├── backtest_engine.py
│   ├── discover_whales.py
│   ├── leaderboard.py
│   └── run_backtest.py
├── src/                        (66 files, 17,901 LOC)
│   ├── main.py                 (orchestrator, 1,409 lines)
│   ├── config.py               (settings loader)
│   ├── metrics.py              (structured JSON logging)
│   ├── alerts/                 (4 files)
│   ├── analysis/               (9 files)
│   ├── core/                   (7 files)
│   ├── dashboard/              (5 files)
│   ├── data/                   (14 files)
│   ├── execution/              (5 files)
│   ├── risk/                   (6 files)
│   ├── storage/                (2 files)
│   ├── strategies/             (7 files)
│   └── scripts/                (3 files)
├── tests/                      (66 files, 13,883 LOC)
├── ecosystem.config.js
├── Makefile
├── pyproject.toml
├── requirements.txt
└── venv/
```

### Module Counts

| Directory | Source Files | Purpose |
|-----------|-------------|---------|
| src/alerts | 4 | Alert dispatch, iMessage, daily reports |
| src/analysis | 9 | Claude forecaster, ensemble, calibration, prompts |
| src/core | 7 | Kalshi/Polymarket clients, models, WebSocket |
| src/dashboard | 5 | FastAPI web dashboard |
| src/data | 14 | Market scanner, news, economic data, whale monitor |
| src/execution | 5 | Order building, routing, fills, positions |
| src/risk | 6 | Risk engine, circuit breaker, Kelly sizer |
| src/storage | 2 | SQLite with WAL mode |
| src/strategies | 7 | AI probability, obvious NO, arb, whale, news |

### Orphaned Files

None found. All 66 source modules are actively imported. The Metaculus client (`metaculus_client.py`) is enabled but degraded (API no longer returns community predictions) with automatic fallback to Manifold.

### Dependencies (requirements.txt)

All 16 production dependencies pinned to exact versions:

| Package | Version | Used By |
|---------|---------|---------|
| kalshi-python | 2.1.4 | Kalshi SDK |
| py-clob-client | 0.34.6 | Polymarket SDK |
| cryptography | 46.0.5 | RSA signing |
| anthropic | 0.86.0 | Claude API |
| httpx | 0.28.1 | HTTP client |
| pyyaml | 6.0.3 | Config |
| pydantic | 2.12.5 | Data models |
| python-dotenv | 1.2.2 | Env loading |
| pytest | 9.0.2 | Testing |
| pytest-asyncio | 1.3.0 | Async tests |
| websockets | 16.0 | WebSocket |
| fastapi | 0.135.1 | Dashboard |
| uvicorn | 0.42.0 | ASGI server |
| jinja2 | 3.1.6 | Templates |
| feedparser | 6.0.12 | RSS feeds |
| ddgs | 9.11.4 | DuckDuckGo search |

No unused dependencies. No known CVEs at time of audit.

### PM2 Ecosystem Config

`ecosystem.config.js` is present and well-configured:
- Auto-restart with max 5 restarts, 10s delay
- Min uptime 10s (prevents restart loops)
- Max memory 500MB (leak protection)
- Kill timeout 30s (graceful shutdown)
- Watch mode disabled (correct for trading bot)
- Env vars parsed from `config/.env` with quote/comment handling

---

## Section 2: Configuration & Environment

### Complete Environment Variable Inventory

| Variable | File | Required | Documented |
|----------|------|----------|------------|
| `KALSHI_API_KEY_ID` | config.py:270 | Yes | Yes |
| `KALSHI_PRIVATE_KEY_PATH` | config.py:271 | Yes | Yes |
| `ANTHROPIC_API_KEY` | config.py:272 | Yes | Yes |
| `SERPER_API_KEY` | config.py:273 | No | Yes |
| `SEARXNG_URL` | config.py:274 | No | Yes |
| `FRED_API_KEY` | config.py:275 | No | Yes |
| `METACULUS_API_TOKEN` | config.py:276 | No | Yes |
| `POLYMARKET_PRIVATE_KEY` | config.py:277 | No | Yes |
| `POLYEDGE_LIVE_ENABLED` | config.py:278 | No | Yes |
| `CONFIRM_NON_US_POLYMARKET` | order_router.py:528 | No | Yes |
| `POLYEDGE_DASHBOARD_KEY` | server.py:85 | No | **No** |
| `POLYEDGE_CORS_ORIGINS` | server.py:72 | No | **No** |

### Findings

- **No hardcoded API keys or secrets** in any source file
- **No secrets in git history** (verified via `git log -G`)
- All API endpoints configurable via `settings.yaml`
- All trading parameters in YAML config, not hardcoded
- `.gitignore` covers: `.env`, `*.pem`, `*.key`, `*.db`, `data/`, `venv/`, `credentials*.json`
- Private key file permissions enforced (0o600) with auto-fix

---

## Section 3: Kalshi Integration

### Endpoints Used (12 total)

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/exchange/status` | GET | No | Health check |
| `/markets` | GET | No | List markets |
| `/markets/{ticker}` | GET | No | Single market |
| `/events` | GET | No | Events with nested markets |
| `/markets/{ticker}/orderbook` | GET | No | Order book |
| `/markets/trades` | GET | No | Trade history |
| `/portfolio/balance` | GET | RSA | Account balance |
| `/portfolio/positions` | GET | RSA | Open positions |
| `/portfolio/orders` | GET | RSA | List orders |
| `/portfolio/orders` | POST | RSA | Create order |
| `/portfolio/orders/{id}` | GET | RSA | Get order |
| `/portfolio/orders/{id}` | DELETE | RSA | Cancel order |

### Authentication

- RSA-PSS signing with `cryptography` library
- Private key loaded from file path in env var
- File permissions validated (0o600 enforced)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- Single retry on 401/403 with 2s backoff
- Key freshness checking available (mtime-based)

### Monetary Calculations

All monetary math uses `Decimal` arithmetic:
- `dollars_to_cents()`: `int(Decimal(str(dollars)).quantize(Decimal("0.01")) * 100)`
- Taker fee: `ceil(0.07 * contracts * price * (1-price))` via Decimal with ROUND_CEILING
- Maker fee: `ceil(0.0175 * contracts * price * (1-price))` via Decimal with ROUND_CEILING
- **No floating-point arithmetic for money**

### Order Placement

- Explicit side mapping: BUY_YES/BUY_NO/SELL_YES/SELL_NO to Kalshi API params
- NO orders converted: `yes_price = dollars_to_cents(1.0 - order.price)`
- Price clamped to [0.01, 0.99] range
- Limit (GTC) and market (FOK) order types supported
- 3-gate safety for live orders (config + env var + interactive confirmation)

### WebSocket

- Channels: `ticker` (prices), `fill` (order fills), `market_lifecycle_v2` (settlements)
- Auto-reconnect: exponential backoff 1s-60s, max 10 consecutive failures
- Ping/pong: 20s ping interval, 30s timeout
- Message ordering caveat documented (M-8): fill may arrive before ticker update
- Settlement detection on reconnect: queries all tracked markets via REST

---

## Section 4: AI Forecasting Pipeline

### Claude Integration

- **Models:** Sonnet 4.6 (routine) / Opus 4.6 (positions >$50)
- **Temperature:** Category-specific (Fed/Macro: 0.20, Politics: 0.25, Geopolitics: 0.30, Tech: 0.30, Culture: 0.40)
- **Max tokens:** 2,000
- **Timeout:** 60s configurable
- **Budget:** 500K tokens/day soft limit, 1M hard limit
- **Cost tracking:** Per-call with Sonnet/Opus pricing

### Prompt Engineering

- Superforecaster-style decomposition explicitly required in system prompt
- Base rate anchoring with historical category resolution rates
- Market price fed directly: `CURRENT MARKET PRICE: {market_price:.0%} (YES)`
- Resolution criteria included verbatim
- Confidence intervals requested (not just point estimates)
- Key uncertainties and factors for/against required
- 3-layer prompt injection protection (truncation, pattern stripping, allowlist)
- 5 category-specific templates: Politics, Fed/Macro, Geopolitics, Tech/AI, Culture

### Response Parsing (4-strategy fallback)

1. Direct JSON parse
2. Markdown code block extraction
3. Brace extraction (first `{` to last `}`)
4. Prose regex (`probability: 0.73` or `probability: 65%`)

Parse failures flagged and rejected (signal not generated).

### Dual-Temperature Cross-Check

Top N signals (by edge) assessed at two temperatures (0.2 and 0.5). If disagreement >22%, CI is widened proportionally. Available but optional.

### No Secondary Models

GPT-4o is **not integrated**. Single-model architecture (Claude only). Ensemble combines Claude + market price + community forecasts (Manifold/Metaculus when available).

---

## Section 5: Data Pipeline & News Integration

### News Research

- **Primary:** DuckDuckGo (free, no API key)
- **Fallback:** Serper.dev (paid, disabled after 3 auth failures, 1h cooldown)
- **Full article text fetched** for top 3 results (not just snippets)
- **HTML extraction:** Robust stdlib parser (skips script/style/nav/footer)
- **Sentence-based truncation** at 3,000 chars max
- **URL deduplication** with tracking parameter stripping
- **Staleness filtering:** Category-aware (Fed: 5 days, Politics: 14 days, Culture: 30 days)

### Data Enrichment

- Concurrent fetching via `asyncio.gather()` with per-source timeouts
- Sources: News (6s), FRED (5s), Cleveland Fed (5s), FedWatch (5s), Community forecasts (4s), Cross-platform (3s)
- Caching: TTL-based (news: 5min, economic: 60min, community: 30min)
- Price-based cache invalidation: >5% move refreshes forecast
- Smart truncation: if combined >5,000 chars, removes shortest sections first

---

## Section 6: Trading Logic & Risk Management

### 11-Point Risk Gate

Every trade must pass ALL checks:

1. **Balance check** -- Available capital minus pending orders
2. **Position size** -- Max 5% bankroll per position
3. **Total exposure** -- Max 40% bankroll
4. **Correlated exposure** -- Max 20% per event category
5. **Circuit breaker** -- Daily loss + consecutive loss checks
6. **Liquidity** -- Order size vs book depth (max 10% of depth)
7. **Existing position** -- Cross-strategy hedge detection
8. **Signal quality** -- Confidence >55%, edge >threshold, probability in valid range
9. **Resolution date** -- Rejects <1 day or >365 days to close
10. **Cooldown** -- Loss exit: 4h cooldown, profit exit: 1h cooldown
11. **Manipulation** -- Rapid price moves, crossed books flagged

### Position Sizing

Half-Kelly with caps:
- Kelly formula: `f = (p*b - q) / b`
- Half-Kelly: `f * 0.5`
- Hard caps: 5% per position, 40% total, 20% correlated
- Liquidity adjustment: halve if >10% of order book
- Fee-accurate sizing via binary search
- Calibration multiplier: Brier >0.28 = halt, >0.22 = quarter sizing

### Exit Conditions (6 triggers)

| Trigger | Threshold |
|---------|-----------|
| Stop loss | 30% unrealized loss |
| Trailing stop | Activates at 12% gain, trails 50% |
| Take profit | 80% of max theoretical gain |
| Time-based | 21 days max hold |
| Edge gone | <20% remaining edge |
| Capital rotation | >35% exposure + <40% edge remaining |

### Circuit Breaker

- Daily loss >10% bankroll: 24-hour halt (auto-reset)
- 3+ consecutive losing days: quarter-Kelly sizing
- 5+ consecutive losing days: full halt (manual intervention)
- State persisted to DB, restored on restart

---

## Section 7: Backtesting & Performance Tracking

### Backtest Engine

- Strategy replay on resolved markets with historical snapshots
- Acknowledged biases: lookahead (degradation factor applied), survivorship (partial mitigation)
- Execution model: 15% fill miss rate, 0-1c slippage, maker at limit price
- Metrics: win rate, profit factor, max drawdown, Sharpe, Calmar, equity curve
- Edge vs return correlation tracking

### Calibration Tracking

- Brier score: `mean((predicted - actual)^2)`, overall and per-category
- Category bias correction: `avg(actual) - avg(predicted)` with statistical significance threshold
- Base rates: historical YES resolution % by category, fed back into prompts
- Minimum 15 samples per category for adjustment
- Confidence interval: `1.96 * sqrt(0.25 / n)` adaptive threshold

---

## Section 8: Error Handling & Reliability

### Exception Handling

- **0 bare `except:` clauses** in production code
- 10 instances of `except Exception:` -- all documented with rationale
- 620+ `logger.*` calls across codebase
- All errors logged with context (never silently swallowed)

### Retry Logic

| Service | Max Retries | Backoff | Cap |
|---------|-------------|---------|-----|
| Kalshi REST | 3 | Exponential (2^n + jitter) | 10s |
| Kalshi Auth (401/403) | 1 | Fixed 2s | N/A |
| Claude API | 3 | Exponential (2^n) | 10s |
| Serper | 2 | Exponential | 30s |

### Circuit Breakers (API-level)

- Kalshi: Opens after 5 consecutive 5xx errors, blocks for 60s
- Claude: Opens after 3 consecutive failures, blocks for 5 minutes
- Timeout tracking: Resets HTTP pool after 3 consecutive timeouts

### State Persistence

- Open positions: loaded from DB on startup
- Pending orders: restored from DB
- Filled order IDs: tracked to prevent duplicate recording
- Partial fills: count preserved across restarts
- Cooldowns: persisted with duration for loss-specific restoration
- Metrics: persisted to DB every cycle

### Memory Management

- `_edge_return_log`: trimmed to 1,000 entries
- Metrics snapshots: kept to last 1,000 rows
- WebSocket callbacks: stable removal with monotonic IDs (warns >50)
- HTTP clients: explicitly closed on error and shutdown
- Cache: TTL-based automatic expiration

### PM2 Restart Recovery

- Auto-restart enabled (max 5, 10s delay)
- Graceful shutdown: 30s kill timeout
- State persistence ensures clean recovery of positions, orders, and cooldowns

---

## Section 9: Security Review

### Credentials

- **No API keys in source code** (verified via grep for sk-ant, api_key, secret, password)
- **No secrets in git history** (verified via `git log -G`)
- All credentials loaded from environment variables
- Private key permissions enforced (0o600)
- No `subprocess` calls in production code (no command injection risk)

### Network Security

- **100% HTTPS** for all API calls (Kalshi, Anthropic, Serper, FRED, Manifold, Polymarket)
- **100% WSS** for WebSocket connections
- Dashboard runs on localhost only (HTTP acceptable for local)
- CORS restricted to localhost by default, configurable via env var

### Data Security

- Database unencrypted (SQLite on disk) -- acknowledged in code comments
- No PII logged in plaintext
- API keys never logged (only success/failure status)
- Log rotation: 10MB max, 5 backups

---

## Section 10: Code Quality

### Large Files (>300 lines)

| File | Lines | Status |
|------|-------|--------|
| storage/database.py | 1,597 | Documented TODO for split |
| main.py | 1,409 | Documented TODO (L-1) |
| execution/order_router.py | 873 | Candidate for split |
| analysis/claude_forecaster.py | 857 | Acceptable (complex domain) |
| execution/position_manager.py | 795 | Candidate for split |
| analysis/news_researcher.py | 762 | Acceptable |
| core/kalshi_client.py | 583 | Acceptable |
| core/websocket_client.py | 542 | Acceptable |
| core/models.py | 542 | Acceptable (Pydantic models) |
| strategies/cross_arb.py | 490 | Acceptable |

### TODO/FIXME Comments

Only 2 in production code:
1. `src/main.py:7` -- TODO L-1: "file is ~1,300 lines and should be split"
2. `src/storage/database.py:283` -- TODO M-18: "Re-enable FK enforcement"

### Code Quality Checks

| Check | Result |
|-------|--------|
| Bare `except:` clauses | 0 found |
| `except Exception:` (broad) | 10 (all documented) |
| Mutable default arguments | 0 found |
| `print()` in production | 0 found |
| Copy-pasted code blocks | None identified |
| Type hints on functions | Comprehensive (Pydantic throughout) |
| Import organization | stdlib / third-party / local |

---

## Section 11: Regulatory Compliance

### Platform Compliance

- **Kalshi** (CFTC-regulated): Primary platform, fully integrated
- **Polymarket**: Secondary platform with explicit jurisdiction gate
  - `CONFIRM_NON_US_POLYMARKET` env var required for live trading
  - Live trades blocked with clear error: "Polymarket is not available to US residents"
- **No circumvention** of platform terms of service

### Trade Record-Keeping

- All trades logged to SQLite with: market_id, direction, price, size, fee, P&L, timestamp
- Calibration records: prediction, outcome, strategy, timestamps
- Metrics: structured JSON logs with cycle stats
- Settlement trades recorded as synthetic close events

### Position Limits

- Per-position: 5% bankroll cap
- Total exposure: 40% bankroll cap
- Correlated: 20% per event category
- No market manipulation mechanisms (no wash trading, no spoofing)

---

## Section 12: Improvement Roadmap Audit

| Feature | Status | Notes |
|---------|--------|-------|
| Feeding Kalshi market price into Claude's prompt | **DONE** | `CURRENT MARKET PRICE: {market_price:.0%}` in all templates |
| GPT-4o as second forecaster | **NOT DONE** | Single-model (Claude only). Community forecasts used instead. |
| Superforecaster-style prompt decomposition | **DONE** | Explicit decomposition method in system prompt |
| Fetching full article text from search results | **DONE** | Top 3 results fetched with HTML parsing |
| Multi-model ensemble with disagreement handling | **PARTIAL** | Claude + market price + community forecasts. No GPT-4o. Dual-temp cross-check available. |
| Calibration tracking with Brier scores | **DONE** | Per-category Brier, bias correction, base rates |
| Performance dashboard | **DONE** | FastAPI dashboard at localhost:8080 with HTMX |

---

## Issues by Severity

### CRITICAL (0 issues)

No critical issues found. The codebase has been through 21 audit revisions with comprehensive fixes.

### HIGH (7 issues)

**H-1: No signal persistence to database**
- **File:** `src/strategies/ai_probability.py`, all strategy files
- **What's wrong:** Signals are generated in-memory, logged at INFO level, but not persisted to a database table with their decision factors (Claude reasoning, edge, confidence, risk gate results)
- **Impact:** Cannot retrospectively audit "Why was this signal generated?" or analyze signal quality vs execution quality. Makes debugging live trading decisions difficult.
- **Fix:** Create a `signals` table with: id, market_id, direction, edge, confidence, reasoning, generated_at, risk_result, status (generated|gated|executed). Log every signal.

**H-2: No maximum concurrent position count**
- **File:** `src/risk/risk_engine.py`
- **What's wrong:** Position limits are exposure-based (5% per, 40% total) but there is no hard cap on number of concurrent positions. Theoretically 8 positions at 5% each = 40%.
- **Impact:** On correlated failures, 8 simultaneous stop-losses could cause slippage clustering, execution failures under load, and cascading forced cancellations. Risk: 2-5% additional loss beyond stop-loss levels.
- **Fix:** Add `max_concurrent_positions: int = 6` config and risk gate check.

**H-3: No maximum drawdown circuit breaker**
- **File:** `src/risk/circuit_breaker.py`
- **What's wrong:** Only daily loss limit (10%) and consecutive losing day tracking. No peak-to-trough drawdown cap (e.g., halt if equity down >20% from all-time high).
- **Impact:** Slow sustained losses over weeks could erode capital without triggering daily limits. A series of 8% daily losses across 3 weeks = ~22% drawdown with no halt.
- **Fix:** Track high-water mark. Add `max_drawdown_pct: 0.20` config. Halt trading if equity drops >20% from peak.

**H-4: Settlement trades bypass fee tracking**
- **File:** `src/execution/position_manager.py` (record_settlement)
- **What's wrong:** Synthetic settlement trades record `fee=0.0`, but the position's accumulated `buy_fees` were never deducted from realized P&L.
- **Impact:** P&L overstated by accumulated buy fees on settled positions. For a $100 position with $1.75 maker fee, P&L overstated by $1.75.
- **Fix:** Set `fee=pos.buy_fees` on synthetic settlement trade to close the fee ledger.

**H-5: Risk gate decisions not structured for analysis**
- **File:** `src/risk/risk_engine.py`
- **What's wrong:** Risk gate pass/fail logged as text strings. Cannot query "How often does balance gate reject?" or "What's typical edge of rejected vs accepted signals?"
- **Impact:** Cannot identify systematic risk gate patterns or tune thresholds based on historical data. Reduces ability to optimize risk parameters.
- **Fix:** Log structured `signal_decisions` records with individual gate pass/fail fields.

**H-6: Non-monotonic fill counts silently skipped**
- **File:** `src/execution/fill_tracker.py` (delta < 0 handling)
- **What's wrong:** If Kalshi API reports a lower `filled_count` (fill correction), the code logs a warning and skips recording the fix, leaving incorrect position size and fees in DB.
- **Impact:** Position size could be overstated, leading to incorrect P&L and risk calculations. Could cause risk engine to approve positions that are actually over-exposed.
- **Fix:** Accept the lower count unconditionally: `_partial_recorded[order.id] = filled_count`.

**H-7: Kalshi key freshness not checked automatically**
- **File:** `src/core/kalshi_client.py`
- **What's wrong:** `check_key_freshness()` exists but is not called automatically in the main scan loop. If the RSA private key is rotated, the bot will use the stale key until manually restarted.
- **Impact:** All authenticated API calls fail after key rotation until bot restart. Could miss trades or fail to manage positions.
- **Fix:** Call `check_key_freshness()` at the start of each scan cycle in `main.py`.

### MEDIUM (14 issues)

**M-1: Extreme-price ensemble weight floor too generous**
- **File:** `src/analysis/ensemble.py` (single-model ensemble)
- **What's wrong:** For markets at <5% or >95%, Claude's effective weight is floored at 0.50. A 900% relative divergence with 50% Claude weight is aggressive.
- **Impact:** Could generate false signals on near-certain markets where Claude hallucinates. Risk: entering positions on markets that shouldn't be traded.
- **Fix:** Lower floor to 0.25 and increase divergence multiplier from 0.5x to 1.0x.

**M-2: No time decay on calibration data**
- **File:** `src/analysis/calibration.py`
- **What's wrong:** Brier score uses ALL resolved predictions with equal weight. 6-month-old predictions weighted same as recent ones.
- **Impact:** Calibration adjustments may reflect stale accuracy patterns. If model improved recently, old poor predictions drag down current calibration.
- **Fix:** Apply exponential decay: `weight = exp(-days_old / 30)` with 30-day half-life.

**M-3: News cache TTL may be stale for breaking news**
- **File:** `src/data/data_enricher.py` (news cache 5 minutes)
- **What's wrong:** Same market assessed twice within 5 minutes gets stale news. For breaking news markets, 5 minutes could mean missing the story.
- **Impact:** Could generate signals based on outdated context, leading to trades that the market has already priced in.
- **Fix:** Reduce news cache TTL to 2-3 minutes for faster freshness.

**M-4: Backtest parameter sweep lacks cross-validation**
- **File:** `scripts/backtest_engine.py`
- **What's wrong:** `--sweep kelly_fraction=0.25,0.5,0.75` tests on FULL dataset. No train/test split.
- **Impact:** Parameter overfitting to historical data. Backtest results may look better than live performance.
- **Fix:** Implement time-series cross-validation with rolling window.

**M-5: Slow manipulation patterns missed**
- **File:** `src/risk/manipulation_detector.py`
- **What's wrong:** Only flags rapid moves (>20% in single interval). Misses sustained pumps (e.g., +5% every 5 min for 30 min = 38% total).
- **Impact:** Could enter positions on markets being manipulated via gradual price inflation.
- **Fix:** Add moving average comparison: flag if 30-minute cumulative move >15%.

**M-6: Crossed book threshold too loose**
- **File:** `src/risk/manipulation_detector.py`
- **What's wrong:** Flags at >8% deviation (YES + NO sum deviates from 1.0). Normal slippage is 1-3%.
- **Impact:** By the time flag triggers, arbitrage opportunity (or manipulation) may already be exploitable.
- **Fix:** Tighten to >5% deviation.

**M-7: Edge threshold not validated against statistical significance**
- **File:** `src/strategies/ai_probability.py`
- **What's wrong:** A 5% edge is accepted regardless of sample size. On a new market with no history, 5% edge could be noise.
- **Impact:** May trade on noise rather than signal, especially on new or low-volume markets.
- **Fix:** Apply Bayesian prior skepticism: require higher edge on markets with <10 trades of history.

**M-8: Excluded categories not enforced downstream**
- **File:** `src/strategies/` (all strategy files)
- **What's wrong:** Market filtering happens at scan time, but no downstream gate re-checks category. If a market somehow passes through, strategies will trade it.
- **Impact:** Could accidentally trade crypto or sports markets with unfavorable fee structures.
- **Fix:** Add `if market.category in EXCLUDED: return None` in all strategy entry points.

**M-9: Selection bias in backtest win rate**
- **File:** `scripts/backtest_engine.py`
- **What's wrong:** Win rate counts only positions resolved during backtest period. Open positions at end are marked-to-price but excluded from win/loss count.
- **Impact:** Biases win rate upward (omits slow losers, includes fast winners). Misleading performance metrics.
- **Fix:** Report `win_rate_realized_only` vs `win_rate_including_unrealized`.

**M-10: Only closed positions tracked for edge-return correlation**
- **File:** `src/metrics.py`
- **What's wrong:** Edge vs return analysis only includes positions that are exited. Risk-gated signals and open positions are excluded.
- **Impact:** Selection bias in performance reporting. Cannot assess whether risk gates are correctly filtering bad signals.
- **Fix:** Track edge separately from returns for all signals (executed, gated, open).

**M-11: Brier score thresholds have gap**
- **File:** `src/risk/kelly_sizer.py`
- **What's wrong:** Brier thresholds: EXCELLENT=0.10, GOOD=0.18, FAIR=0.22, POOR=0.28. Score of 0.19 falls to GOOD (1.0x) instead of being partially penalized.
- **Impact:** Marginal calibration (0.19-0.21) may get full position sizing when it should be reduced.
- **Fix:** Use continuous scaling instead of step function, or adjust GOOD threshold to 0.20.

**M-12: Dashboard defaults to unauthenticated**
- **File:** `src/dashboard/server.py:85`
- **What's wrong:** `POLYEDGE_DASHBOARD_KEY` is optional. Without it, dashboard exposes portfolio data without authentication.
- **Impact:** Anyone on the local network could view positions, P&L, and trading activity.
- **Fix:** Require auth by default, allow disabling via explicit env var `POLYEDGE_DASHBOARD_NO_AUTH=true`.

**M-13: Two env vars undocumented in .env.example**
- **File:** `config/.env.example`
- **What's wrong:** `POLYEDGE_DASHBOARD_KEY` and `POLYEDGE_CORS_ORIGINS` not listed in .env.example.
- **Impact:** Users may not know about dashboard authentication or CORS configuration.
- **Fix:** Add both with comments to `.env.example`.

**M-14: Balance sync only per scan cycle**
- **File:** `src/main.py` (_sync_bankroll)
- **What's wrong:** Balance fetched once per scan cycle (default 300s). Could miss rapid changes from manual deposits/withdrawals or other systems.
- **Impact:** Risk engine could approve orders based on stale balance. Unlikely to cause issues at current trade frequency.
- **Fix:** Add balance pre-flight check immediately before order submission in `order_router.py`.

### LOW (8 issues)

**L-1: main.py is 1,409 lines (documented TODO)**
- **File:** `src/main.py:7`
- **What's wrong:** Orchestrator file should be split into startup, scan_cycle, trade_cycle, lifecycle modules.
- **Fix:** Refactor into 4 focused modules per the existing TODO comment.

**L-2: database.py is 1,597 lines**
- **File:** `src/storage/database.py`
- **What's wrong:** Largest file in codebase. Contains schema, migrations, and all query methods.
- **Fix:** Split into schema.py, migrations.py, queries.py.

**L-3: Foreign key enforcement disabled**
- **File:** `src/storage/database.py:283` (TODO M-18)
- **What's wrong:** `PRAGMA foreign_keys = ON` is commented out to avoid migration issues.
- **Fix:** Re-enable after verifying all foreign key relationships.

**L-4: Polymarket jurisdiction gate only on live trades**
- **File:** `src/execution/order_router.py:528`
- **What's wrong:** Paper trading allows Polymarket without `CONFIRM_NON_US_POLYMARKET` check.
- **Fix:** Apply gate to paper trading for consistency.

**L-5: Token budget estimate is approximate**
- **File:** `src/analysis/claude_forecaster.py:213`
- **What's wrong:** Estimated 3K tokens per call, actual varies 1.5-4.5K. Could hit hard limit slightly before expected.
- **Fix:** Log actual token usage over 100+ calls and refine estimate.

**L-6: Dashboard route files lack unit test isolation**
- **File:** `src/dashboard/routes_api.py`, `routes_html.py`, `routes_partials.py`
- **What's wrong:** Tested via integration through `server.py` but no isolated unit tests.
- **Fix:** Add isolated unit tests for each route file.

**L-7: No disk space monitoring**
- **File:** N/A
- **What's wrong:** Rotating logs (10MB x 5 backups) + SQLite database could fill drive over time. No alert when disk < 10%.
- **Fix:** Add disk space check in main loop, alert when below threshold.

**L-8: Calibration predictions not logged for non-traded markets**
- **File:** `src/analysis/calibration.py`
- **What's wrong:** Only markets that generate signals contribute to calibration. Arb-only or risk-gated markets don't log predictions, causing survivorship bias.
- **Fix:** Log prediction for every market assessed, regardless of whether a signal was generated.

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|--------|-------------|--------------|----------------|---------------|---------------|---------|
| src/core/kalshi_client.py | 5 | 5 | 5 | 5 | 4 | **4.8** |
| src/core/models.py | 5 | 5 | 5 | N/A | 4 | **4.8** |
| src/core/websocket_client.py | 4 | 4 | 5 | 4 | 4 | **4.2** |
| src/core/market_discovery.py | 5 | 5 | 4 | 4 | 4 | **4.4** |
| src/core/polymarket_client.py | 4 | 4 | 4 | 4 | 3 | **3.8** |
| src/analysis/claude_forecaster.py | 5 | 5 | 5 | 5 | 4 | **4.8** |
| src/analysis/prompt_templates.py | 5 | 5 | N/A | N/A | 5 | **5.0** |
| src/analysis/ensemble.py | 4 | 4 | 4 | 4 | 4 | **4.0** |
| src/analysis/calibration.py | 4 | 4 | 4 | 4 | 4 | **4.0** |
| src/analysis/news_researcher.py | 5 | 4 | 5 | 4 | 4 | **4.4** |
| src/data/market_scanner.py | 5 | 5 | 4 | 4 | 4 | **4.4** |
| src/data/data_enricher.py | 5 | 4 | 5 | 4 | 4 | **4.4** |
| src/data/fred_client.py | 4 | 4 | 4 | N/A | 3 | **3.8** |
| src/execution/order_builder.py | 5 | 5 | 5 | 5 | 4 | **4.8** |
| src/execution/order_router.py | 4 | 4 | 5 | 5 | 4 | **4.4** |
| src/execution/fill_tracker.py | 4 | 4 | 4 | 4 | 4 | **4.0** |
| src/execution/position_manager.py | 4 | 4 | 4 | 4 | 4 | **4.0** |
| src/risk/risk_engine.py | 5 | 5 | 5 | 5 | 4 | **4.8** |
| src/risk/circuit_breaker.py | 5 | 5 | 4 | 5 | 4 | **4.6** |
| src/risk/kelly_sizer.py | 5 | 5 | 4 | 5 | 4 | **4.6** |
| src/risk/manipulation_detector.py | 3 | 4 | 3 | 3 | 3 | **3.2** |
| src/strategies/ai_probability.py | 5 | 5 | 4 | 5 | 4 | **4.6** |
| src/strategies/obvious_no.py | 5 | 5 | 4 | 4 | 4 | **4.4** |
| src/strategies/cross_arb.py | 4 | 4 | 4 | 4 | 4 | **4.0** |
| src/strategies/news_reactive.py | 4 | 4 | 4 | 4 | 3 | **3.8** |
| src/storage/database.py | 4 | 4 | 4 | 4 | 3 | **3.8** |
| src/main.py | 3 | 4 | 4 | 5 | 3 | **3.8** |
| src/dashboard/server.py | 4 | 3 | 4 | 3 | 3 | **3.4** |
| src/alerts/alert_manager.py | 4 | 4 | 4 | N/A | 4 | **4.0** |
| src/config.py | 5 | 5 | 4 | 4 | 4 | **4.4** |
| src/metrics.py | 5 | 5 | 4 | N/A | 4 | **4.5** |

**Average: 4.2/5.0**

---

## Top 10 Recommendations (Prioritized)

### 1. Add signal persistence to database (H-1)
**Category:** Risk reduction + Reliability
Signals are ephemeral -- cannot audit why a trade was made after the fact. Create a `signals` table logging every signal with reasoning, edge, confidence, and risk gate results. Critical for debugging live trading and improving strategy performance.

### 2. Add maximum concurrent position count (H-2)
**Category:** Risk reduction
Without a hard cap, 8 simultaneous 5% positions hitting stop-loss could cascade. Add `max_concurrent_positions: 6` risk gate. Simple change, large downside protection.

### 3. Add maximum drawdown circuit breaker (H-3)
**Category:** Risk reduction
Daily limits don't catch slow sustained losses across weeks. Track high-water mark and halt at 20% drawdown from peak. Protects against regime changes the daily limit doesn't catch.

### 4. Fix settlement fee accounting (H-4)
**Category:** Risk reduction (accuracy)
Settlement trades recording `fee=0.0` overstates P&L. Set `fee=pos.buy_fees` on synthetic settlement. One-line fix with direct impact on reported performance.

### 5. Structure risk gate logging (H-5)
**Category:** Reliability + Performance
Text-based risk gate logs cannot be queried or analyzed. Add structured per-gate pass/fail records. Enables data-driven risk parameter tuning.

### 6. Fix non-monotonic fill handling (H-6)
**Category:** Risk reduction
Fill count corrections from Kalshi API are silently skipped, leaving stale position data. Accept corrections unconditionally to maintain position accuracy.

### 7. Auto-check key freshness (H-7)
**Category:** Reliability
RSA key rotation goes undetected until manual restart. Add `check_key_freshness()` call at start of each scan cycle.

### 8. Tighten manipulation detection (M-5, M-6)
**Category:** Risk reduction
Slow pump patterns and loose crossed-book thresholds leave gaps. Add 30-minute cumulative move check and tighten book deviation from 8% to 5%.

### 9. Add calibration time decay (M-2)
**Category:** Performance
Equal-weight calibration drags recent improvements down with old data. Exponential decay with 30-day half-life keeps calibration current.

### 10. Refactor main.py and database.py (L-1, L-2)
**Category:** Code quality
Two files over 1,400 lines each. Split into focused modules per existing TODO comments. Improves maintainability and reduces merge conflicts.

---

## Overall Assessment

**PolyEdge is a mature, well-engineered trading system** that has been through 21+ audit revisions. The codebase demonstrates strong financial engineering discipline:

- **Security:** Excellent. No credential leaks, HTTPS everywhere, multi-gate live trading safety.
- **Reliability:** Strong. Comprehensive retry logic, circuit breakers, state persistence, graceful degradation.
- **Risk Controls:** Strong. 11-point risk gate, Half-Kelly sizing, circuit breakers, manipulation detection.
- **Code Quality:** Good. Well-modularized, fully tested (930/933 pass), minimal tech debt.
- **AI Pipeline:** Strong. Superforecaster prompts, calibration tracking, category-aware temperature tuning.

The 7 HIGH issues are operational improvements rather than fundamental flaws. None would cause immediate financial loss, but all should be addressed before extended live trading. The most impactful fixes are signal persistence (H-1) for post-trade analysis and maximum drawdown protection (H-3) for capital preservation.

**Issue Count:** 0 Critical | 7 High | 14 Medium | 8 Low

**Verdict: Ready for paper trading. Address H-1 through H-4 before sustained live trading.**
