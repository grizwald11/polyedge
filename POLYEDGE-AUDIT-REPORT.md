# PolyEdge — Complete Codebase Audit Report

**Date:** 2026-03-28
**Auditor:** Claude Opus 4.6 (automated deep audit)
**Scope:** All 127 Python files (62 source, 65 test), configs, scripts
**Platform:** Kalshi (CFTC-regulated) + Polymarket (non-US only, gated)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Source files** | 62 (.py in src/) |
| **Test files** | 65 (.py in tests/) |
| **Total source LOC** | ~15,470 |
| **Total test LOC** | ~12,317 |
| **Total tests collected** | 835 |
| **Tests passing** | 818 (per last commit) |
| **Test-to-code ratio** | 0.80 |
| **External API integrations** | 4 (Kalshi, Anthropic, Serper/DDG, Metaculus) + 4 data (FRED, Cleveland Fed, FedWatch, Manifold) |
| **Env vars total** | 10 |
| **Env vars documented** | 10/10 (all in .env.example) |
| **Dependencies (pinned)** | 17 pinned, 5 optional/commented |
| **Trading mode** | Paper (safe default) |
| **Bankroll configured** | $5,000 |

---

## Issues by Severity

### CRITICAL — Fix Before Next Live Trade (3 issues)

**C-1: Double Circuit Breaker Multiplier — Positions Under-Sized**
- **File:** `src/main.py:475-478`
- **What:** `circuit_breaker.get_kelly_multiplier()` is applied in `main.py` AFTER Kelly sizer already applies it internally via `set_circuit_breaker_multiplier()`. The reduction is applied twice.
- **Impact:** During drawdowns, positions are sized at 0.25x x 0.5x = 0.125x Kelly instead of intended 0.25x. Under-trading loses expected profits ($50-200/month on $5K bankroll).
- **Fix:** Remove the manual multiplication in `main.py:475-478`. Kelly sizer already incorporates the circuit breaker multiplier.

**C-2: 50-Minute Position Desync in Live Mode**
- **File:** `src/main.py:712-718`
- **What:** `sync_with_kalshi()` only runs every 10 cycles. At 5-min intervals, that's 50 minutes between syncs. If a fill occurs at minute 1, the position manager won't know until minute 50.
- **Impact:** Could attempt to sell contracts not yet recorded locally, or double-enter a market. Potential for naked short orders or doubled positions ($250+ exposure error).
- **Fix:** Call `sync_with_kalshi()` after every fill detection (after line 107), not just every 10 cycles.

**C-3: Calibration Adjustments Computed But Never Applied**
- **File:** `src/analysis/calibration_analyzer.py:95-131` computes per-category bias adjustments
- **What:** `get_category_adjustments()` returns bias corrections (e.g., "Claude underestimates Politics by 5%") but no code ever calls this to adjust forecasts. The calibration feedback loop described in CLAUDE.md is broken.
- **Impact:** Systematic prediction bias persists indefinitely. If Claude consistently overestimates Fed markets by 8%, every Fed trade carries hidden negative edge. Over 100 trades, this could mean $200-500 in avoidable losses.
- **Fix:** In `claude_forecaster.assess_market()`, after getting the result, apply `adjusted_prob = claude_prob + category_adjustments[category]`. Then pass adjusted probability to ensemble.

---

### HIGH — Fix This Week (8 issues)

**H-1: Stale Prices Used for Stop-Loss Decisions**
- **File:** `src/execution/position_manager.py:355-361`
- **What:** `should_exit()` skips capital rotation if price data is >5 min old, but stop-loss and trailing stop logic still execute on stale data. A price that was $0.60 five minutes ago could now be $0.45.
- **Impact:** False stop-loss exits on stale data, or missed stops when prices actually dropped. Could realize $50-100 unnecessary losses per false exit.
- **Fix:** Require fresh price data (<2 min) for ALL exit decisions, not just capital rotation.

**H-2: Obvious-NO Edge Calculation Underestimates Returns**
- **File:** `src/strategies/obvious_no.py:88-94`
- **What:** Edge formula uses `probability_estimate - no_price` but for near-certain markets (YES=$0.01, NO=$0.99), the true return is `(1.0 - 0.99) / 0.99 = 1.01%`, while the formula gives `0.995 - 0.99 = 0.5%`. Edge is underestimated by ~50%.
- **Impact:** Kelly sizer under-sizes obvious-NO positions. Opportunity cost of $20-50/month on missed sizing.
- **Fix:** For obvious-NO, calculate edge as return-on-investment rather than probability difference.

**H-3: Prompt Injection Partially Sanitized**
- **File:** `src/analysis/prompt_templates.py:234-264`
- **What:** `_sanitize_external_text()` detects prompt injection patterns in market descriptions and news articles, replaces them with `[REMOVED]`, but surrounding malicious text remains. A crafted market description could still influence Claude's probability estimate.
- **Impact:** Adversarial market creators could manipulate Claude's forecasts, causing trades at bad prices. Theoretical loss depends on position size (up to $250).
- **Fix:** Use strict allowlist (alphanumeric + standard punctuation) for market descriptions. Hard-reject descriptions containing control phrases. Move sanitization before prompt construction.

**H-4: 8 Bare `except Exception:` Clauses Without Logging**
- **Files:** `news_ingestion.py:170`, `cross_arb.py:444`, `claude_forecaster.py:278`, `news_researcher.py:510,523`, `dashboard/server.py:258`, `main.py:405,1142`
- **What:** These catch blocks silently swallow exceptions without any logging. Failures are invisible.
- **Impact:** Bugs in production become impossible to diagnose. A silent failure in arb relationship caching (`cross_arb.py:444`) could cause repeated false signals.
- **Fix:** Add `logger.debug()` or `logger.warning()` to every bare except block.

**H-5: Cross-Check Logic Exists But Is Never Called**
- **File:** `src/analysis/claude_forecaster.py:361-480`
- **What:** `cross_check_assess()` runs two concurrent Claude calls at different temperatures to validate high-stakes forecasts. It's fully implemented but never invoked from any strategy or orchestrator.
- **Impact:** High-stakes positions (>$50) don't get the dual-temperature validation designed for them. Reduces forecast confidence on largest trades.
- **Fix:** Wire up in `ai_probability.py` for positions exceeding high-stakes threshold.

**H-6: News Impact Template Unused — News-Reactive Strategy Incomplete**
- **File:** `src/analysis/prompt_templates.py` defines `NEWS_IMPACT_TEMPLATE`
- **What:** Template for "breaking news -> probability shift assessment" is defined but never called. The news-reactive strategy (`src/strategies/news_reactive.py`) exists but doesn't use this specialized prompt.
- **Impact:** News-reactive signals use generic assessment instead of impact-specific analysis, reducing signal quality.
- **Fix:** Route breaking news through `NEWS_IMPACT_TEMPLATE` in `news_reactive.py`.

**H-7: No Forecast Caching — Redundant Claude API Calls**
- **File:** `src/analysis/claude_forecaster.py`
- **What:** Same market can be assessed multiple times within a scan cycle (different strategies, retries). No caching of recent forecasts.
- **Impact:** Wastes API tokens ($0.003-0.015 per call). At 50 markets x 300s cycle, could waste $5-15/day in duplicate calls.
- **Fix:** Cache forecasts keyed by `(market_ticker, 5min_bucket)`. `src/data/cache.py` already exists with TTL support but isn't integrated.

**H-8: ARB Validation Template Never Used**
- **File:** `src/analysis/prompt_templates.py` defines `ARB_VALIDATION_TEMPLATE`
- **What:** Cross-market arbitrage signals skip Claude validation of the logical relationship. Two markets with subtly different resolution criteria could trigger false arb signals.
- **Impact:** False arbitrage trades on markets that aren't actually logically related. Could lose full position ($125-250).
- **Fix:** Call `ARB_VALIDATION_TEMPLATE` before executing any cross-market arb in `cross_arb.py`.

---

### MEDIUM — Fix When Possible (12 issues)

**M-1: Float Arithmetic for Monetary Calculations**
- **Files:** Throughout (`position_manager.py`, `risk_engine.py`, `order_builder.py`, `kelly_sizer.py`)
- **What:** All monetary values use IEEE 754 float with `round()` mitigation. Not using `Decimal`.
- **Impact:** At current scale ($5K bankroll, $250 max position), float errors are <$0.01. At $50K+ bankroll, accumulated drift could reach $0.10-1.00 per day.
- **Fix:** Migrate to `Decimal` for all monetary calculations when scaling up. Current `round()` approach is adequate for Phase 3-4.

**M-2: `storage/database.py` Is 1,470 Lines — Monolithic**
- **File:** `src/storage/database.py`
- **What:** Single file handles schema, connection, market CRUD, snapshot logging, signal tracking, order management, trade logging, calibration records, whale tracking, cross-platform pairs, and metrics aggregation.
- **Impact:** Hard to maintain, test in isolation, or find bugs. Any change risks breaking unrelated functionality.
- **Fix:** Split into `database_core.py` (connection/migrations), `database_markets.py`, `database_trades.py`, `database_calibration.py`.

**M-3: Price Clamping Silently Hides Upstream Bugs**
- **File:** `src/execution/order_builder.py:186-191`
- **What:** `_clamp_price()` silently clamps prices to [0.01, 0.99]. If an edge calculation produces `probability = 1.05`, it becomes 0.99 with no warning.
- **Impact:** Masks logic bugs that could cause bad trades. A probability > 1.0 indicates a calculation error upstream.
- **Fix:** Log a warning when clamping is triggered. Consider raising an exception for values > 1.1 or < -0.1.

**M-4: No Data Enricher Caching**
- **File:** `src/data/data_enricher.py`
- **What:** Every market assessment triggers fresh API calls to FRED, Cleveland Fed, FedWatch, Metaculus, etc. No caching between cycles.
- **Impact:** Wastes API quota and adds 5-15s latency per assessment. FRED data changes daily at most; fetching every 5 minutes is unnecessary.
- **Fix:** Cache enricher results: FRED (60min TTL), FedWatch (60min), Metaculus (30min), News (5min).

**M-5: Circuit Breaker Not Logged at CRITICAL Level**
- **File:** `src/risk/circuit_breaker.py`
- **What:** Trading halt events use `logger.warning()`. These are the most severe operational events.
- **Fix:** Use `logger.critical()` for halt events and `logger.error()` for reduced sizing.

**M-6: `logger.exception()` Underutilized**
- **Files:** `polymarket_client.py:68`, `order_router.py:407,518`, `dashboard/server.py:258`, `claude_forecaster.py:292`
- **What:** These use `logger.error(f"...: {e}")` instead of `logger.exception()`, losing the full stack trace.
- **Fix:** Replace with `logger.exception()` for automatic traceback capture.

**M-7: Multi-Model Ensemble Is Dead Code**
- **File:** `src/analysis/ensemble.py:106-193`
- **What:** `multi_model_ensemble()` is fully implemented (Brier-score-weighted, disagreement penalty) but never called. Only single-model `ensemble_forecast()` is used.
- **Impact:** No second model to catch Claude's systematic errors. CLAUDE.md requires "second probability estimate from different approach."
- **Fix:** Implement base-rate model or Metaculus community forecast as second input to `multi_model_ensemble()`.

**M-8: Context Truncation Drops Low-Priority Data Silently**
- **File:** `src/data/data_enricher.py:160-174`
- **What:** When context exceeds 5,000 chars, entire sections are removed from the end (lowest priority first). Polymarket cross-ref data is typically dropped first.
- **Impact:** Cross-platform price differences (the strongest mispricing evidence) may be silently removed from Claude's context.
- **Fix:** Re-rank all sections by relevance to the specific market before truncation, or increase limit.

**M-9: No Timeout on DuckDuckGo Search**
- **File:** `src/analysis/news_researcher.py:163-208`
- **What:** DDG search runs in executor but has no explicit timeout. If the DDG library hangs, the entire research function blocks.
- **Fix:** Wrap in `asyncio.wait_for(..., timeout=8)`.

**M-10: Serper API No Retry on 5xx Errors**
- **File:** `src/analysis/news_researcher.py:210-239`
- **What:** On `httpx.HTTPError` that isn't 401/403/400, the code logs and falls back to DDG permanently. A temporary 503 error triggers permanent fallback.
- **Fix:** Add 1-2 retries with exponential backoff for 5xx errors before falling back.

**M-11: Max Divergence Threshold Defined But Not Enforced**
- **File:** `src/config.py:117`
- **What:** `max_divergence_from_market: 0.40` is configured but never checked in `claude_forecaster.py`. If Claude says 85% and market says 10% (75% divergence), no alarm is raised at the forecaster level.
- **Impact:** Hallucinated forecasts pass through to ensemble. The strategy-level divergence gate (`ai_probability.py:272-291`) partially catches this, but not for all strategies.
- **Fix:** Add divergence check in `claude_forecaster.py` that flags extreme divergence in the ForecastResult.

**M-12: No Post-Fill Slippage Monitoring**
- **File:** `src/execution/order_router.py`
- **What:** Live fills report actual prices, but there's no comparison against expected price. If market moves 3% between risk check and fill, the trade may have negative expected value.
- **Fix:** After each live fill, compare actual price to expected. Warn if divergence > 1%.

---

### LOW — Optional Improvements (8 issues)

**L-1:** Magic numbers in `order_router.py` (0.15 miss rate, 0.01 slippage, 3600 TTL) should be named constants.

**L-2:** `parse_market()` in `market_discovery.py` is 175 lines. Should be broken into helpers (`_parse_prices()`, `_parse_tokens()`, etc.).

**L-3:** `dashboard/server.py` defines entire FastAPI app in 460 lines. Routes should be split into modules.

**L-4:** `pytest-asyncio==1.3.0` is very old. Latest is 0.24+. Should upgrade for better async test support.

**L-5:** No performance benchmarks for scan cycle latency, signal generation time, or order placement speed.

**L-6:** No stress testing for 100+ simultaneous positions or rapid market movements.

**L-7:** `http://` URLs in dashboard logging (`server.py:453`, `main.py:1141`) — not a security issue since it's localhost, but should note it's HTTP-only.

**L-8:** No mutation testing to verify test quality beyond line coverage.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS per-request | All endpoints | 3x exponential backoff | Semaphore(5) + 100ms min | 30s default | Comprehensive | Production-ready |
| Kalshi WebSocket | Via REST auth headers | Auto-reconnect | 1-60s backoff | Subscription dedup | Explicit SSL context | Good | Production-ready |
| Anthropic (Claude) | API key from env | 4-tier parse fallback | 3x backoff, market-price fallback | Token budget tracking | 60s explicit | Good | Production-ready |
| Serper (Search) | API key from env | Falls back to DDG | No retry on 5xx | 1hr cooldown on auth fail | 10s explicit | Basic | Needs 5xx retry |
| DuckDuckGo | No auth needed | Falls back gracefully | No retry | N/A (free) | No explicit timeout | Basic | Needs timeout |
| FRED | API key from env | Optional, graceful | No retry | N/A | httpx default | Basic | OK (optional) |
| Metaculus | Token from env | Optional, graceful | No retry | N/A | httpx default | Basic | OK (optional) |
| Manifold | No auth needed | Optional, graceful | No retry | N/A | httpx default | Basic | OK (optional) |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi events API, pagination, filtering | 14 tests | Volume/liquidity/category filters | Good |
| Forecast Generation | Claude with category templates, market price in prompt | 20+ tests | No calibration feedback loop (C-3) | Needs fix |
| Edge Detection | forecast - market_price, min edge gates | 22+ tests | Category Brier gating, divergence limits | Good |
| Position Sizing | Half-Kelly with caps (5% position, 40% total) | 17 tests | Double CB multiplier (C-1) | Needs fix |
| Order Execution | Paper + Live with 3-gate safety | 18 tests | Three-gate system, 15s timeout with reconciliation | Good |
| Position Tracking | Weighted avg entry, fee tracking, P&L | 24 tests | 50-min sync gap in live (C-2) | Needs fix |
| P&L Calculation | Realized + unrealized, buy/sell fees | Covered in position tests | Rounding to 4dp | Good |
| Settlement Handling | All Kalshi statuses recognized, WebSocket lifecycle | Covered in discovery tests | Value validation [0,1] | Good |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|---|---|---|---|---|---|---|
| **src/core/kalshi_client.py** | 5 | 5 | 5 | 5 | 4 | **5** |
| **src/core/models.py** | 5 | 5 | 4 | N/A | 4 | **5** |
| **src/core/market_discovery.py** | 4 | 4 | 5 | 4 | 3 | **4** |
| **src/core/websocket_client.py** | 5 | 4 | 5 | 5 | 4 | **5** |
| **src/core/polymarket_client.py** | 4 | 4 | 4 | 4 | 3 | **4** |
| **src/analysis/claude_forecaster.py** | 4 | 4 | 5 | 3 | 4 | **4** |
| **src/analysis/prompt_templates.py** | 4 | 4 | 3 | 3 | 4 | **4** |
| **src/analysis/ensemble.py** | 5 | 4 | 4 | 4 | 4 | **4** |
| **src/analysis/calibration.py** | 5 | 5 | 4 | N/A | 4 | **5** |
| **src/analysis/calibration_analyzer.py** | 4 | 4 | 4 | 3 | 3 | **4** |
| **src/analysis/news_researcher.py** | 4 | 3 | 3 | 3 | 3 | **3** |
| **src/analysis/resolution_tracker.py** | 4 | 4 | 4 | 4 | 3 | **4** |
| **src/execution/order_builder.py** | 4 | 5 | 4 | 4 | 4 | **4** |
| **src/execution/order_router.py** | 4 | 4 | 5 | 5 | 4 | **4** |
| **src/execution/position_manager.py** | 4 | 5 | 4 | 3 | 4 | **4** |
| **src/execution/fill_tracker.py** | 4 | 5 | 4 | 4 | 3 | **4** |
| **src/strategies/ai_probability.py** | 5 | 5 | 4 | 5 | 4 | **5** |
| **src/strategies/cross_arb.py** | 4 | 4 | 3 | 4 | 3 | **4** |
| **src/strategies/obvious_no.py** | 3 | 4 | 4 | 4 | 3 | **4** |
| **src/strategies/whale_tracker.py** | 4 | 4 | 4 | 4 | 3 | **4** |
| **src/strategies/news_reactive.py** | 3 | 3 | 3 | 3 | 3 | **3** |
| **src/strategies/cross_platform_arb.py** | 4 | 3 | 4 | 4 | 3 | **4** |
| **src/risk/risk_engine.py** | 5 | 5 | 4 | 5 | 4 | **5** |
| **src/risk/kelly_sizer.py** | 5 | 5 | 4 | 5 | 4 | **5** |
| **src/risk/circuit_breaker.py** | 4 | 5 | 4 | 5 | 4 | **5** |
| **src/risk/portfolio_risk.py** | 4 | 5 | 4 | 4 | 3 | **4** |
| **src/storage/database.py** | 3 | 5 | 4 | N/A | 3 | **3** |
| **src/data/market_scanner.py** | 4 | 4 | 4 | 4 | 3 | **4** |
| **src/data/data_enricher.py** | 4 | 3 | 4 | 3 | 3 | **3** |
| **src/data/news_ingestion.py** | 3 | 3 | 3 | 3 | 3 | **3** |
| **src/data/market_graph.py** | 4 | 4 | 4 | N/A | 3 | **4** |
| **src/data/whale_monitor.py** | 4 | 4 | 4 | 4 | 3 | **4** |
| **src/alerts/alert_manager.py** | 4 | 4 | 5 | N/A | 3 | **4** |
| **src/dashboard/server.py** | 3 | 4 | 4 | N/A | 3 | **3** |
| **src/main.py** | 4 | 4 | 4 | 4 | 4 | **4** |
| **src/config.py** | 5 | 5 | 4 | 5 | 4 | **5** |

Scale: 1=Poor, 2=Below Average, 3=Adequate, 4=Good, 5=Excellent

---

## Section 1: Structural Integrity

### Directory Tree

```
polyedge/
├── CLAUDE.md (933 lines — project spec)
├── POLYEDGE-AUDIT-PROMPT.md (252 lines)
├── Makefile
├── ecosystem.config.js (pm2)
├── pyproject.toml
├── requirements.txt (17 pinned + 5 optional)
├── .gitignore
├── config/
│   ├── settings.yaml (100 lines)
│   ├── categories.yaml (50 lines)
│   ├── .env.example (35 lines)
│   └── .env (secrets — gitignored)
├── src/ (62 files, ~15,470 lines)
│   ├── main.py (1,223 lines — orchestrator)
│   ├── config.py (262 lines)
│   ├── metrics.py (153 lines)
│   ├── core/ (7 files, 2,182 lines)
│   ├── analysis/ (9 files, 2,417 lines)
│   ├── execution/ (5 files, 1,976 lines)
│   ├── data/ (14 files, 2,259 lines)
│   ├── strategies/ (7 files, 1,512 lines)
│   ├── risk/ (5 files, 802 lines)
│   ├── storage/ (2 files, 1,470 lines)
│   ├── dashboard/ (2 files, 460 lines)
│   └── alerts/ (4 files, 256 lines)
├── tests/ (65 files, ~12,317 lines)
│   ├── conftest.py (229 lines — shared fixtures)
│   ├── test_core/ (10 files)
│   ├── test_analysis/ (9 files)
│   ├── test_execution/ (6 files)
│   ├── test_data/ (12 files)
│   ├── test_strategies/ (7 files)
│   ├── test_risk/ (5 files)
│   ├── test_alerts/ (4 files)
│   ├── test_dashboard/ (2 files)
│   ├── test_scripts/ (4 files)
│   └── test_integration/ (2 files)
├── scripts/ (5 files, 1,422 lines)
│   ├── backtest_engine.py (686 lines)
│   ├── backfill_markets.py (330 lines)
│   ├── run_backtest.py (270 lines)
│   ├── discover_whales.py (71 lines)
│   └── leaderboard.py (65 lines)
└── data/ (gitignored)
    ├── markets.db
    ├── chroma/
    └── logs/
```

### File Counts by Directory

| Directory | Source Files | Test Files |
|-----------|-------------|------------|
| core/ | 7 | 10 |
| analysis/ | 9 | 9 |
| execution/ | 5 | 6 |
| data/ | 14 | 12 |
| strategies/ | 7 | 7 |
| risk/ | 5 | 5 |
| storage/ | 2 | (covered in test_core) |
| dashboard/ | 2 | 2 |
| alerts/ | 4 | 4 |
| scripts/ | 5 | 4 |
| root (main, config, metrics) | 3 | 3 |

### Orphaned/Dead Code

| Item | Location | Status |
|------|----------|--------|
| `cross_check_assess()` | `claude_forecaster.py:361-480` | Implemented, never called (H-5) |
| `multi_model_ensemble()` | `ensemble.py:106-193` | Implemented, never called (M-7) |
| `NEWS_IMPACT_TEMPLATE` | `prompt_templates.py` | Defined, never used (H-6) |
| `ARB_VALIDATION_TEMPLATE` | `prompt_templates.py` | Defined, never used (H-8) |
| `get_category_adjustments()` | `calibration_analyzer.py:95-131` | Computes results, never applied (C-3) |
| `max_divergence_from_market` | `config.py:117` | Configured, never enforced (M-11) |

### Config & Dependency Verification

- `ecosystem.config.js` — Correct pm2 config, loads .env, auto-restart (max 5)
- `.env.example` — All 10 env vars documented with helpful comments
- `requirements.txt` — 17 dependencies pinned to exact versions
- `pyproject.toml` — Project metadata present
- `Makefile` — Common commands defined
- All 17 declared dependencies are actually used in source code
- Optional deps (chromadb, pandas, numpy) handled with graceful ImportError fallback

---

## Section 2: Configuration & Environment

### Complete Environment Variable Registry

| Variable | Required | Source | Purpose |
|----------|----------|--------|---------|
| `KALSHI_API_KEY_ID` | Conditional (Kalshi) | config.py:252 | Kalshi API authentication |
| `KALSHI_PRIVATE_KEY_PATH` | Conditional (Kalshi) | config.py:253 | Path to RSA private key |
| `ANTHROPIC_API_KEY` | **Required** | config.py:254 | Claude API authentication |
| `POLYMARKET_PRIVATE_KEY` | Conditional (Polymarket) | config.py:258 | Polymarket wallet key |
| `SERPER_API_KEY` | Optional | config.py:255 | Serper search API |
| `SEARXNG_URL` | Optional | config.py:256 | Alternative search backend |
| `FRED_API_KEY` | Optional | config.py:257 | Federal Reserve economic data |
| `METACULUS_API_TOKEN` | Optional | config.py:259 | Metaculus forecast API |
| `POLYEDGE_LIVE_ENABLED` | Optional | config.py:260, order_router.py | Safety gate for live trading |
| `CONFIRM_NON_US_POLYMARKET` | Optional | order_router.py:423 | Polymarket jurisdiction gate |

- All 10 vars documented in `.env.example`
- All loaded centrally through `src/config.py` (no scattered `os.environ` calls)
- No hardcoded secrets found in source code (verified via grep)
- No API keys committed to git history
- All API endpoint URLs configurable via `settings.yaml`
- Paper/live mode switching via config + env var gate

---

## Section 3: Kalshi Integration

### Endpoints Used

| Endpoint | Method | Purpose | Auth | Error Handling |
|----------|--------|---------|------|----------------|
| `/events?with_nested_markets=true` | GET | Market discovery | Yes | Pagination + empty check |
| `/markets/{ticker}` | GET | Single market detail | Yes | Null-safe |
| `/markets/{ticker}/orderbook` | GET | Order book depth | Yes | Try/except with logging |
| `/markets/trades?ticker=T` | GET | Recent trades | Yes | Pagination |
| `/portfolio/balance` | GET | Account balance | Yes | Cents-to-dollars, negative clamp |
| `/portfolio/positions` | GET | Open positions | Yes | Null-safe extraction |
| `/portfolio/orders` | POST | Place order | Yes | 15s timeout + reconciliation |
| `/portfolio/orders/{id}` | GET | Order status | Yes | Fill tracking |
| `/portfolio/orders/{id}` | DELETE | Cancel order | Yes | Graceful on failure |

### Authentication

- RSA-PSS signing with SHA256 + PSS.MAX_LENGTH salt — matches Kalshi spec
- Timestamp in milliseconds (13-digit epoch)
- Full path includes `/trade-api/v2` prefix for signing
- Private key file permission validated (0o600, auto-fix with warning)
- No session management needed (per-request signing)

### Rate Limiting

- `asyncio.Semaphore(5)` limits concurrent requests
- 100ms minimum interval between requests
- 3 retries with exponential backoff (2s, 4s, 8s, capped at 10s) + jitter
- Returns `None` on exhaustion (no crash)

### Order Placement

- Price clamping [0.01, 0.99] with cent conversion (`int(round(dollars * 100))`)
- YES/NO price correctly complemented (`1.0 - price` for NO side)
- `kalshi_side` explicitly stored in Order model (no fragile inference)
- GTC=limit (maker, 175 bps fee), FOK=market (taker, 700 bps fee)
- 15s timeout on order creation with `_reconcile_after_timeout()` fallback

### Monetary Calculations

- Uses float with consistent rounding (4dp for fees/costs, 6dp for averages)
- Integer cents at API boundary (`dollars_to_cents()` / `cents_to_dollars()`)
- Fee calculation uses `math.ceil()` on cent values (rounds up, conservative)
- See M-1 for Decimal migration recommendation

---

## Section 4: AI Forecasting Pipeline

### Claude Integration

- **Market price in prompt:** Every template includes `CURRENT MARKET PRICE: {market_price:.0%}`
- **Superforecaster decomposition:** System prompt instructs AND/OR/conditional decomposition
- **Category-specific templates:** 6 templates (Politics, Fed/Macro, Geopolitics, Tech/AI, Culture, General)
- **Two-tier model selection:** Sonnet for <$50, Opus for >=$50
- **Category-specific temperature:** 0.20 (Fed) to 0.45 (Culture)
- **4-tier response parsing:** JSON -> code block -> brace extraction -> prose pattern matching
- **Token budget tracking:** Soft limit with daily accounting
- **Resolution criteria validation:** Caution message added for ambiguous criteria
- Calibration feedback not applied (C-3)
- Cross-check not wired up (H-5)
- Prompt injection partially sanitized (H-3)

### Ensemble Logic

- Single-model ensemble: Claude 85% / Market 15% base weights
- Adaptive penalties: CI width, divergence, extreme price adjustments
- Multi-model ensemble ready (dead code, needs second model) (M-7)
- Disagreement penalty is multiplicative (not additive)
- Calibration adjustments not applied to weights (C-3)

### Data Pipeline

- **Dual-backend search:** DuckDuckGo primary, Serper fallback
- **Full article text fetching:** HTML-to-text extraction for top 3 results
- **Freshness filtering:** Category-aware max ages (5d Fed, 14d Politics)
- **Deduplication:** Jaccard similarity on title words (>70% threshold)
- **7+ concurrent data sources:** News, FRED, Cleveland Fed, FedWatch, Metaculus, Manifold, Polymarket
- **15s hard timeout** with partial result preservation
- No caching between cycles (M-4)
- No timeout on DDG search (M-9)

---

## Section 5: Data Pipeline & News Integration

### Serper API

- API key from environment, 10s HTTP timeout
- Auth failure triggers 1-hour cooldown, falls back to DDG
- No retry on 5xx errors (M-10)
- Full article text fetched (not just snippets) for top results
- Search query construction: 2-4 multi-angle queries per market
- Relevance scoring: keyword overlap + recency bonus

### Data Freshness

- Category-aware staleness filtering (5d-30d depending on category)
- Relative date parsing ("2 days ago" -> absolute)
- No tracking of how old market price is when fed to Claude

---

## Section 6: Trading Logic & Risk Management

### Trading Decision Engine

- **Edge threshold:** 5% min for AI probability, 2% for arbitrage, 1% for obvious-NO
- **Category Brier gating:** Categories with Brier > 0.30 skipped; 0.20-0.30 requires 8% edge
- **Divergence gate:** Rejects forecasts >40% from market; tighter (25%) for extreme prices
- **Max trades per cycle:** 5 (configurable)
- **Deduplication:** Only highest-edge signal per market per cycle

### Position Sizing

- **Half-Kelly:** `f = 0.5 * (p*b - q) / b`
- **Hard caps:** 5% bankroll per position, 40% total exposure, 20% correlated
- **Min 1 contract:** For any viable position
- **Reject <$0.10 contracts:** Prevents outsized loss risk on cheap contracts
- **Calibration multiplier:** 0.1x-1.0x based on Brier score
- Double CB multiplier bug (C-1)

### Risk Management

- **10-point risk check:** Balance, position size, total exposure, correlated exposure, circuit breaker, liquidity, existing position, confidence, edge minimum, resolution date, cooldown
- **Circuit breaker:** 10% daily loss halts trading; 3 consecutive losing days -> quarter-Kelly; 5 consecutive -> full halt
- **Cooldown:** 1-hour re-entry cooldown per market after exit
- **Obvious-NO cap:** Max 10% of bankroll in obvious-NO positions
- **Stop-loss:** Configurable per-position (default 30%)
- **Trailing stop:** Activates at 12% unrealized gain
- **Time-based exit:** Max 21 days hold

### Three-Gate Safety System

- **Gate 1:** `trading.mode: "live"` in settings.yaml (default: "paper")
- **Gate 2:** `POLYEDGE_LIVE_ENABLED=true` env var (default: false)
- **Gate 3:** Interactive console confirmation on first live trade per session (60s timeout)
- **Polymarket gate:** Additional `CONFIRM_NON_US_POLYMARKET` check

---

## Section 7: Backtesting & Performance Tracking

### Backtest Infrastructure

- `scripts/run_backtest.py` — Replay historical trades, compute metrics
- `scripts/backtest_engine.py` — Monte Carlo simulation, drawdown analysis, Sharpe ratio
- Metrics: Win rate, profit factor, max drawdown, Brier score, P&L time series
- Filter by strategy, days, date range
- Does NOT re-run Claude (uses historical signals only — no market replay)
- Does not account for slippage beyond paper-trade simulation

### Calibration Tracking

- `log_prediction()` records forecast before resolution
- `resolve_prediction()` updates with actual outcome
- Brier score calculation (correct formula: mean((predicted - actual)^2))
- 10-bin calibration curve data
- Per-category Brier scores and bias analysis
- Win rate and profit factor by strategy
- Dual-platform resolution checking (Kalshi + Polymarket)
- Calibration adjustments computed but not applied (C-3)

### Logging

- All trading decisions logged with: forecast, market price, edge, confidence, reasoning
- Entry price, size, fees, strategy, model used all recorded
- Timestamps in UTC
- No prompt version tracking (can't A/B test templates)

---

## Section 8: Error Handling & Reliability

### Try/Except Quality

- 110 proper `except Exception as e:` blocks with logging
- 8 bare `except Exception:` without logging (H-4)
- All ImportError handlers log gracefully for optional dependencies
- All critical API paths have timeout + retry logic

### Graceful Degradation

| Scenario | Behavior | Status |
|----------|----------|--------|
| Anthropic API down | Falls back to market price with `parse_failed=True` | Good |
| Kalshi API down | Returns None, risk engine blocks trades | Good |
| Serper down | Falls back to DuckDuckGo | Good |
| DDG down | Continues without news context | Good |
| Internet drops mid-trade | 15s timeout + reconciliation | Good |
| Bot crashes and restarts | Fills tracked in DB, no double-recording | Good |
| WebSocket disconnects | Auto-reconnect 1-60s backoff + REST polling fallback | Good |

### State Persistence

- SQLite with WAL mode for concurrent access
- All positions, orders, trades persisted to DB
- Circuit breaker state persisted across restarts
- Cooldown timers persisted
- `_load_filled_order_ids()` prevents double-recording on restart

### Memory Leak Risks

- `_max_seen_urls = 10000` cap on news URL dedup set
- `_processed_fills` set bounded by order lifecycle
- No explicit cleanup of old WebSocket subscriptions (tickers for closed markets) — minor risk

---

## Section 9: Security Review

### Credential Exposure

- No secrets in source code (verified via grep for `sk-ant`, `api_key`, `secret`, `password`)
- `.gitignore` covers: `.env`, `config/.env`, `*.pem`, `*.key`, `config/kalshi_private_key*`, `credentials*.json`
- No secrets in git history
- Private key file permission validated (0o600)
- Live credentials exist in working directory (`config/.env`, `config/kalshi_private_key.pem`) — recommend `chmod 600`

### HTTPS Verification

- All API endpoints use `https://` (Kalshi, Anthropic, Serper, FRED, Metaculus, Manifold)
- WebSocket uses `wss://` with explicit SSL context
- No `http://` in API calls (only in localhost dashboard logging)

### Injection Risks

- No `subprocess`, `os.system`, `eval()`, or `exec()` calls in source code
- No SQL injection risk (parameterized queries via SQLAlchemy)
- Prompt injection partially mitigated (H-3)

### TODO/FIXME/HACK/XXX Comments

- **None found.** Zero TODO/FIXME/HACK/XXX comments in source code.

### Print Statements

- **None found.** Zero `print()` calls in source code. All output uses `logging`.

---

## Section 10: Code Quality

### Functions Over 50 Lines

| Function | File | Lines | Recommendation |
|----------|------|-------|----------------|
| `parse_market()` | `market_discovery.py` | 175 | Split into helpers |
| `parse_polymarket_market()` | `polymarket_discovery.py` | 176 | Split into helpers |
| `create_app()` | `dashboard/server.py` | 423 | Split routes into modules |
| `main()` | `main.py` | ~200 | Acceptable (orchestrator) |

### Files Over 300 Lines

| File | Lines | Recommendation |
|------|-------|----------------|
| `storage/database.py` | 1,470 | Split into modules (M-2) |
| `main.py` | 1,223 | Acceptable (well-structured) |
| `execution/order_router.py` | 763 | Acceptable (focused) |
| `execution/position_manager.py` | 622 | Acceptable |
| `analysis/claude_forecaster.py` | 576 | Acceptable |
| `analysis/news_researcher.py` | 524 | Acceptable |
| `core/models.py` | 501 | Acceptable (model defs) |

### Code Patterns

- No mutable default arguments
- Consistent f-string usage (no `.format()` mixing)
- Imports organized: stdlib -> third-party -> local
- Type hints on all function signatures
- Logging uses proper levels (486 calls: 45% INFO, 27% WARNING, 14% ERROR, 14% DEBUG)
- No `print()` statements
- No TODO/FIXME/HACK/XXX comments
- 6 magic numbers not extracted to constants (L-1)
- `logger.exception()` underutilized (M-6)

---

## Section 11: Regulatory Compliance

### Platform Verification

- **Primary platform is Kalshi** (CFTC-regulated, legal for US users)
- **Polymarket access gated** by `CONFIRM_NON_US_POLYMARKET` env var
- Polymarket orders blocked by default with explicit warning: "Polymarket residency gate: set CONFIRM_NON_US_POLYMARKET=true to confirm non-US residency"
- Polymarket currently `enabled: false` in settings.yaml

### Trading Compliance

- No market manipulation mechanisms (no wash trading, spoofing, or layering)
- Position limits enforced (5% per position, 40% total)
- All trades logged with full detail (timestamp, price, size, fees, strategy, market)
- SQLite database preserves complete trade history for tax reporting
- Daily P&L reports generated automatically

### Terms of Service

- No attempt to circumvent Kalshi rate limits (proper throttling)
- No scraping of Kalshi UI (uses official API only)
- Leaderboard access uses public Polymarket endpoints only

---

## Section 12: Improvement Roadmap Audit

| Feature | Status | Notes |
|---------|--------|-------|
| Feeding Kalshi market price into Claude's prompt | **Implemented** | Every template includes market price |
| GPT-4o as second forecaster for ensemble | **Not implemented** | `multi_model_ensemble()` ready but no second model integrated |
| Superforecaster-style prompt decomposition | **Implemented** | AND/OR/conditional instructions in system prompt |
| Fetching full article text from search results | **Implemented** | HTML-to-text extraction for top 3 results |
| Multi-model ensemble with disagreement handling | **Code ready, not wired** | `multi_model_ensemble()` + disagreement penalty implemented, never called |
| Calibration tracking with Brier scores | **Implemented** | Full logging, Brier calculation, category breakdown |
| Calibration feedback loop | **Not implemented** | Adjustments computed but never applied (C-3) |
| Performance dashboard | **Implemented** | FastAPI with portfolio, calibration, strategy views + JSON API |
| News-reactive trading | **Partial** | Strategy exists, but NEWS_IMPACT_TEMPLATE unused (H-6) |
| Cross-market arb validation | **Partial** | ARB_VALIDATION_TEMPLATE exists but unused (H-8) |
| Cross-check (dual-temperature) | **Code ready, not wired** | `cross_check_assess()` implemented, never called (H-5) |

---

## Top 10 Recommendations (Prioritized)

### 1. Fix Double Circuit Breaker Multiplier (C-1)
**Risk:** Under-sizing all positions during drawdowns
**Effort:** 5 minutes — Remove 3 lines in `main.py:475-478`
**Impact:** Correct position sizing = proper risk-reward

### 2. Sync Positions After Every Fill in Live Mode (C-2)
**Risk:** 50-minute position desync could cause naked shorts or double entries
**Effort:** 15 minutes — Move `sync_with_kalshi()` call to after fill detection
**Impact:** Prevents $250+ exposure errors

### 3. Apply Calibration Adjustments to Forecasts (C-3)
**Risk:** Systematic bias persists indefinitely, eroding edge
**Effort:** 30 minutes — Add adjustment in `claude_forecaster.assess_market()`
**Impact:** Closes the calibration feedback loop; estimated 3-8pp Brier improvement

### 4. Fix Stale Price Exit Decisions (H-1)
**Risk:** False stop-loss exits or missed stops on old data
**Effort:** 15 minutes — Add freshness check before all exit conditions
**Impact:** Prevents $50-100 per false exit

### 5. Wire Up Cross-Check for High-Stakes Trades (H-5)
**Risk:** Largest positions get no additional validation
**Effort:** 30 minutes — Call `cross_check_assess()` in `ai_probability.py` for positions >$50
**Impact:** Higher confidence on largest positions

### 6. Harden Prompt Injection Filtering (H-3)
**Risk:** Adversarial market descriptions could manipulate Claude forecasts
**Effort:** 1 hour — Implement strict allowlist for external text
**Impact:** Prevents adversarial manipulation of trading signals

### 7. Add Forecast Caching (H-7)
**Risk:** Wasted API tokens on duplicate assessments
**Effort:** 30 minutes — Integrate existing `src/data/cache.py`
**Impact:** Save $5-15/day in API costs

### 8. Fix Obvious-NO Edge Calculation (H-2)
**Risk:** Under-sizing most profitable low-risk strategy
**Effort:** 15 minutes — Use return-on-investment formula
**Impact:** Better sizing = $20-50/month additional returns

### 9. Add Bare Except Logging (H-4)
**Risk:** Silent failures make production debugging impossible
**Effort:** 20 minutes — Add logging to 8 except blocks
**Impact:** Dramatically faster incident diagnosis

### 10. Implement Data Enricher Caching (M-4)
**Risk:** Redundant API calls waste time and quota
**Effort:** 1 hour — Add TTL caching for FRED, FedWatch, Metaculus
**Impact:** 5-15s faster assessments, reduced API usage

---

## Conclusion

PolyEdge is a **well-architected, comprehensively tested trading system** with strong fundamentals. The codebase demonstrates:

- **Excellent risk management:** 10-point risk checks, half-Kelly sizing, circuit breakers, three-gate safety
- **Robust API integration:** Proper auth, retry logic, rate limiting, graceful degradation
- **Comprehensive testing:** 835 tests at 0.80 test-to-code ratio with edge case coverage
- **Clean code practices:** No print statements, no TODOs, organized imports, type hints throughout
- **Security discipline:** No hardcoded secrets, proper .gitignore, no injection vectors

The **3 critical issues** (double CB multiplier, position desync, calibration feedback gap) should be fixed before live trading with real capital. The **8 high-priority issues** should be addressed within the first week of operation. The system is otherwise **production-ready for paper trading** and can safely transition to live trading after these fixes.

**Overall Grade: A- (91/100)**

The primary gap is not in what's built, but in what's built-but-not-connected: calibration feedback, cross-checking, arb validation, and multi-model ensemble are all implemented but not wired into the main pipeline. Connecting these existing components would significantly improve forecast quality and reduce risk.
