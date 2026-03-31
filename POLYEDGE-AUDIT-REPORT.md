# PolyEdge Complete Codebase Audit Report

**Date:** March 31, 2026
**Auditor:** Claude Code (claude-opus-4-6)
**Codebase:** /Users/adamgrodin/polyedge
**Commit:** f8a71d2 (main)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Total source files** | 81 Python modules |
| **Total lines of production code** | 23,842 |
| **Total test files** | 77 |
| **Total test functions** | 1,476 |
| **Total lines of test code** | 22,980 |
| **Test-to-code ratio** | 96% |
| **External API integrations** | 8 (Kalshi, Polymarket, Anthropic, Serper, DuckDuckGo, FRED, Metaculus, Cleveland Fed) |
| **Environment variables** | 12 total, 12 documented in .env.example |
| **Dependencies** | 16 pinned, 0 unused |
| **Trading mode** | Paper (default) |
| **Primary exchange** | Kalshi (CFTC-regulated) |

### Test Coverage by Module

| Module | Files | Lines | Tests | Coverage Estimate |
|--------|-------|-------|-------|-------------------|
| src/core | 8 | 3,255 | 196 | 85% |
| src/analysis | 20 | 6,903 | 419 | 80% |
| src/data | 16 | 2,981 | 129 | 70% |
| src/strategies | 7 | 2,226 | 83 | 75% |
| src/execution | 5 | 2,512 | 153 | 85% |
| src/risk | 6 | 1,656 | 136 | 90% |
| src/orchestrator | 5 | 1,633 | 66 | 75% |
| src/storage | 2 | 1,795 | 84 | 80% |
| src/dashboard | 5 | 615 | 71 | 60% |
| src/alerts | 4 | 256 | 14 | 55% |
| src/metrics | 1 | 260 | 13 | 70% |

---

## Issues by Severity

### CRITICAL (fix before next trade)

#### C-1. Balance Handling Uses Float Instead of Decimal
- **File:** `src/core/kalshi_client.py:485`
- **What's wrong:** Balance is converted using `float(raw_balance) / 100.0`. Floating-point arithmetic introduces rounding errors on monetary values.
- **Impact:** On a $5,000 account over hundreds of transactions, accumulated rounding errors could corrupt position tracking and risk calculations, potentially allowing oversized positions. Worst case: cents-to-dollars discrepancy causing a risk check to pass when it shouldn't.
- **Fix:** Use `Decimal(str(raw_balance)) / Decimal(100)` for the internal calculation. The rest of the system (position_manager.py) already uses Decimal for P&L — this is an inconsistency at the source.

#### C-2. Verify Kalshi Side/Action Parameter Mapping
- **File:** `src/execution/order_router.py:408-416`
- **What's wrong:** Orders are submitted with both `side` ("yes"/"no") and `action` ("buy"/"sell") parameters. The mapping from internal Signal direction to these two Kalshi API parameters needs verification against Kalshi docs for all 4 combinations (buy-yes, buy-no, sell-yes, sell-no).
- **Impact:** If the combination is misinterpreted, orders execute on the WRONG SIDE. A buy-yes could become a sell-no or vice versa. On a $250 position, this is a potential $500 swing (you lose $250 AND gain an unwanted $250 exposure in the wrong direction).
- **Fix:** Add integration test that verifies all 4 side/action combinations against Kalshi's demo API. Add assertion in order_builder that validates the mapping.

#### C-3. No Market Status Check Before Order Submission
- **File:** `src/execution/order_router.py:408` (Kalshi), `src/execution/order_router.py:271` (Polymarket)
- **What's wrong:** Orders are submitted without verifying the market is open. The Market model has `closed` and `status` fields, but neither is checked before calling `create_order()`.
- **Impact:** Orders submitted to closed/settled/halted markets will be rejected by the API, but the system may have already committed resources (pending balance reservation, position tracking state). Repeated failures could trigger the circuit breaker unnecessarily.
- **Fix:** Add `if market and (market.closed or market.status in ("closed", "settled", "halted", "determined")): return rejected` before order submission in both `_live_fill()` and `_poly_live_fill()`.

---

### HIGH (fix this week)

#### H-1. Balance Pre-Flight Check Fails Open
- **File:** `src/execution/order_router.py:385-401`
- **What's wrong:** If the balance check times out or throws an exception, the order proceeds anyway:
  ```python
  except (asyncio.TimeoutError, Exception) as e:
      logger.debug(f"Balance pre-flight check skipped: {e}")
      # Continue anyway
  ```
- **Impact:** Orders could be submitted with insufficient funds, leading to API rejections. For large orders (>10% bankroll), this is a risk.
- **Fix:** For orders above a configurable threshold (e.g., 10% bankroll), make the balance check blocking with retries. Only skip for small orders.

#### H-2. Rate Limiting Is Reactive, Not Proactive
- **File:** `src/core/kalshi_client.py:227-259`
- **What's wrong:** Rate limit handling waits for 429 responses, then backs off. Multiple concurrent requests can all hit 429 simultaneously (thundering herd).
- **Impact:** Burst scan cycles could exhaust rate limits, causing missed trading opportunities during the backoff period.
- **Fix:** Implement a token bucket rate limiter (e.g., `asyncio.Semaphore` with time-based refill) that throttles requests proactively.

#### H-3. Price Comparison Uses Float with Fixed Tolerance
- **File:** `src/execution/order_router.py:460`
- **What's wrong:** `abs(oo.get("yes_price", 0) / 100 - order.price) < 0.01` uses float comparison. On low-price markets ($0.02 vs $0.03), the 0.01 tolerance is 50% of the price.
- **Impact:** False positive order reconciliation matches — the system could incorrectly associate an unrelated fill with an order.
- **Fix:** Compare in integer cents: `abs(int(oo.get("yes_price", 0)) - int(round(order.price * 100))) <= 1`.

#### H-4. WebSocket Connection State Race Condition
- **File:** `src/core/websocket_client.py:241, 285, 308`
- **What's wrong:** `self._ws` is assigned and cleared without explicit locking. The `connected` property (line 323) reads `_ws` concurrently.
- **Impact:** In rare cases during reconnection, a callback could read `_ws` as non-None while it's being replaced, leading to sending on a closing connection.
- **Fix:** Protect `_ws` assignment with `asyncio.Lock()`.

#### H-5. Missing Platform Field Falls Back Silently
- **File:** `src/execution/fill_tracker.py:81, 232, 325, 386`
- **What's wrong:** `getattr(order, "platform", Platform.KALSHI)` silently defaults to KALSHI when the platform attribute is missing.
- **Impact:** A Polymarket order missing the platform field would be tracked/filled as a Kalshi order. Position reconciliation would break.
- **Fix:** Log a warning when fallback is used, and ensure all Order construction paths set platform explicitly.

#### H-6. Polymarket Fee Calculation Is Stubbed
- **File:** `src/core/models.py:73-80`
- **What's wrong:** `polymarket_fee()` always returns 0.0 with a warning comment. If fee-enabled Polymarket markets are ever traded, P&L will be wrong.
- **Impact:** Currently safe (Polymarket disabled and event markets are fee-free), but becomes a live bug if Polymarket is enabled for fee-bearing markets.
- **Fix:** Implement the actual fee calculation before enabling Polymarket for any fee-bearing market categories. Add a guard that rejects fee-enabled markets while the stub is active.

#### H-7. WebSocket Key Refresh Not Implemented
- **File:** `src/core/websocket_client.py:90-103`
- **What's wrong:** The private key is loaded once at WebSocket connection time. The REST client has `check_key_freshness()`, but the WebSocket doesn't call it. If the key file is rotated while connected, reconnection will fail.
- **Impact:** After key rotation, WebSocket reconnects fail with 401/403, losing real-time price feeds. Position manager would operate on stale prices, potentially triggering false exits.
- **Fix:** Call `kalshi.check_key_freshness()` before each reconnection attempt.

---

### MEDIUM (fix when possible)

#### M-1. Silent Exception in Database Query
- **File:** `src/storage/database.py:1528`
- **What's wrong:** `except Exception: return None` with no logging. The comment says "Table may not exist yet" but this catches ALL exceptions silently.
- **Impact:** Database corruption, schema mismatch, or lock contention would be invisible — making production debugging nearly impossible.
- **Fix:** Add `logger.debug(f"Query failed (table may not exist): {e}")`.

#### M-2. Polymarket Discovery Has No Retry Logic
- **File:** `src/core/polymarket_discovery.py:261-308`
- **What's wrong:** API calls use httpx defaults with no explicit retry on failure.
- **Impact:** Transient Polymarket API failures could cause missed market discovery. Low priority since Polymarket is currently disabled.
- **Fix:** Add retry logic consistent with kalshi_client.py pattern.

#### M-3. Retry Logic Pattern Duplicated Across 3 Files
- **Files:** `src/core/kalshi_client.py:210-260`, `src/analysis/news_researcher.py:352-427`, `src/analysis/claude_forecaster.py:371-413`
- **What's wrong:** Exponential backoff retry logic is copy-pasted with minor variations across three files.
- **Impact:** Maintenance burden — fixing a retry bug requires updating 3 places.
- **Fix:** Extract into `src/core/retry_helper.py` with configurable parameters.

#### M-4. Calibration Adjustment Applied After Ensemble
- **File:** `src/strategies/ai_probability.py:566`
- **What's wrong:** Category bias adjustments are applied AFTER ensemble averaging. If consensus sources already incorporate similar bias, this double-counts.
- **Impact:** Systematic over/under-correction in categories where consensus sources share Claude's bias. Could lead to 1-3% edge miscalculation.
- **Fix:** Apply adjustments per-model BEFORE ensemble blending, or add a dampening factor when consensus sources are present.

#### M-5. Partial Fill Validation Accepts Non-Monotonic Corrections
- **File:** `src/execution/fill_tracker.py:197-229`
- **What's wrong:** Non-monotonic fill counts are accepted with only a warning. A buggy API response (e.g., 1000 -> 500 -> 2000) could corrupt position size tracking.
- **Impact:** Position sizes could be wrong, leading to incorrect P&L and risk calculations.
- **Fix:** Add bounds check — reject corrections larger than 5% of order size, escalate with alert.

#### M-6. Token Estimation Heuristic Is Fixed
- **File:** `src/analysis/claude_forecaster.py:237`
- **What's wrong:** `ESTIMATED_CALL_TOKENS = 3000` is a fixed heuristic. Actual token usage varies by market category and prompt complexity.
- **Impact:** Budget tracking could be off by 30-50%, leading to premature budget exhaustion or unexpected overspend.
- **Fix:** Track rolling average of actual token usage and use that for estimation.

#### M-7. Large Files Could Benefit from Splitting
- **Files:** `src/storage/database.py` (1,743 lines), `src/analysis/claude_forecaster.py` (1,003 lines), `src/execution/order_router.py` (967 lines)
- **What's wrong:** These files exceed reasonable module size. database.py contains schema, migrations, and all query methods in one file.
- **Impact:** Increased cognitive load and merge conflict risk during development.
- **Fix:** Split database.py into core + queries. Extract JSON parsing from claude_forecaster.py.

#### M-8. Type Hint Coverage at 61%
- **What's wrong:** ~39% of functions lack return type hints, particularly in data clients and dashboard routes.
- **Impact:** Reduced IDE support, harder to catch type errors.
- **Fix:** Add return type hints incrementally, prioritizing execution and risk modules.

#### M-9. No Integration Test for Full Risk+Kelly+CircuitBreaker Flow
- **What's wrong:** Each risk component is tested individually, but no single test exercises a trade flowing through all three systems together.
- **Impact:** Interaction bugs between components could be missed.
- **Fix:** Add a test that generates a signal, sizes it through Kelly, checks it through risk engine, and verifies circuit breaker interaction.

#### M-10. Serper Disables Globally After Single Auth Failure
- **File:** `src/analysis/news_researcher.py`
- **What's wrong:** After a single 4xx error, Serper is disabled for 1 hour globally. A transient auth issue blocks all Serper-dependent queries.
- **Impact:** News context quality degrades for 1 hour on any transient failure.
- **Fix:** Use per-query retry before global disable; require 3 consecutive auth failures to trigger cooldown.

---

### LOW (optional improvements)

#### L-1. TODO in Whale Monitor
- **File:** `src/data/whale_monitor.py:183-185`
- **What's wrong:** `_log_whale_trade()` bypasses the Database abstraction layer, using raw SQL via `self.db._get_conn()`.
- **Fix:** Add `Database.log_whale_trade()` method.

#### L-2. Edge Threshold for Model Selection Is Hardcoded
- **File:** `src/analysis/claude_forecaster.py:90-91`
- **What's wrong:** `if abs(edge) > 0.15` is hardcoded rather than using config.
- **Fix:** Use `self.settings.claude.edge_highstakes_threshold`.

#### L-3. WebSocket Callback List Unbounded
- **File:** `src/core/websocket_client.py:182, 198`
- **What's wrong:** `_reconnect_callbacks` list has `MAX_RECONNECT_CALLBACKS = 50` constant defined but enforcement is unclear.
- **Fix:** Add explicit check when adding callbacks.

#### L-4. Whale Monitor Position Dict Unbounded
- **File:** `src/data/whale_monitor.py:49-50`
- **What's wrong:** `_positions` dictionary grows without explicit cleanup.
- **Fix:** Prune inactive wallets periodically.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS signing | Comprehensive (HTTPStatusError, RequestError) | 3 retries, exponential backoff | Reactive (429 + Retry-After) | 30s | 196 tests | GOOD |
| Kalshi WebSocket | RSA-PSS signed upgrade | Multi-level retry, auth failure detect | Auto-reconnect | N/A | ping 30s | Included in core tests | GOOD |
| Anthropic (Claude) | API key header | Rate limit, auth error, timeout | 3 retries + circuit breaker | Budget-based (500k tokens/day) | 60s configurable | 419 tests (analysis) | EXCELLENT |
| Serper (Search) | API key param | Auth failure cooldown | 2 retries, exponential | 429 handling | 10s | Included in analysis tests | GOOD |
| DuckDuckGo | None | Fallback to Serper | Implicit (ddgs library) | Library-managed | Library default | Included in analysis tests | FAIR |
| FRED | API key param | Sanitized URL logging | 3 retries | None explicit | 10s | 129 data tests | GOOD |
| Metaculus | Bearer token | Timeout + connect error | 3 retries | None explicit | 10s | Included in data tests | GOOD |
| Cleveland Fed | None | Standard httpx | None explicit | None | 15s | Included in data tests | FAIR |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi events API + filtering by category/volume/liquidity | 196 core tests | Category exclusion (Crypto, Sports) at scanner + risk engine | GOOD |
| Forecast Generation | Claude with category-specific prompts, superforecaster decomposition, dual-temperature cross-check | 419 analysis tests | Circuit breaker, token budget, confidence gates, divergence gates | EXCELLENT |
| Edge Detection | Ensemble-weighted (Claude 85% + market 15%), calibration adjustment, market efficiency scaling | Included in strategy tests | Min edge threshold (5% AI, 2% arb), Brier-gated category skip | EXCELLENT |
| Position Sizing | Half-Kelly with 3-layer caps (5% position, 40% total, 20% correlated), liquidity adjustment, confidence scaling, calibration multiplier | 118 Kelly tests | Extreme price rejection (<3c, >97c), fee-adjusted caps | EXCELLENT |
| Order Execution | Paper + live routing, maker preference, 60s timeout, order reconciliation | 153 execution tests | Three-gate live safety (config + env var + confirmation), balance pre-flight | GOOD |
| Position Tracking | Decimal P&L, 5 exit triggers (stop-loss, trailing, take-profit, max-hold, edge-gone), staleness checks | 47 exit logic tests | 2-minute price staleness guard, slippage buffer | GOOD |
| P&L Calculation | Realized + unrealized tracking, per-strategy breakdown, daily aggregation | 84 storage tests | Decimal arithmetic in position_manager | GOOD |
| Settlement Handling | Synthetic trade records, P&L calculation on settlement, position cleanup | Included in position mgr tests | Decimal settlement P&L | GOOD |

---

## Module-by-Module Scorecard

### src/core/ (API Clients & Models)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| models.py | 549 | 5/5 | 4/5 | 5/5 | 4/5 | 4/5 | 4.4/5 |
| kalshi_client.py | 600 | 4/5 | 4/5 | 4/5 | 3/5 | 4/5 | 3.8/5 |
| polymarket_client.py | 243 | 3/5 | 3/5 | 3/5 | 2/5 | 3/5 | 2.8/5 |
| websocket_client.py | 539 | 4/5 | 3/5 | 4/5 | 3/5 | 4/5 | 3.6/5 |
| market_discovery.py | 340 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |
| polymarket_discovery.py | 310 | 3/5 | 3/5 | 2/5 | 2/5 | 3/5 | 2.6/5 |
| key_loader.py | 83 | 4/5 | 2/5 | 4/5 | 4/5 | 3/5 | 3.4/5 |

### src/analysis/ (Forecasting)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| claude_forecaster.py | 1,003 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5.0/5 |
| news_researcher.py | 818 | 4/5 | 4/5 | 4/5 | 3/5 | 4/5 | 3.8/5 |
| prompt_templates.py | 416 | 5/5 | 4/5 | N/A | N/A | 5/5 | 4.7/5 |
| ensemble.py | 339 | 5/5 | 4/5 | 4/5 | 4/5 | 5/5 | 4.4/5 |
| calibration.py | 379 | 5/5 | 4/5 | 4/5 | 4/5 | 5/5 | 4.4/5 |
| calibration_analyzer.py | 362 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |
| decomposer.py | 422 | 5/5 | 4/5 | 4/5 | 4/5 | 5/5 | 4.4/5 |

### src/strategies/ (Trading Strategies)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| ai_probability.py | 751 | 5/5 | 4/5 | 4/5 | 5/5 | 5/5 | 4.6/5 |
| cross_arb.py | 521 | 4/5 | 3/5 | 3/5 | 3/5 | 4/5 | 3.4/5 |
| obvious_no.py | 133 | 4/5 | 3/5 | 3/5 | 4/5 | 4/5 | 3.6/5 |
| whale_tracker.py | 193 | 3/5 | 3/5 | 3/5 | 3/5 | 3/5 | 3.0/5 |
| news_reactive.py | 171 | 3/5 | 3/5 | 3/5 | 3/5 | 3/5 | 3.0/5 |

### src/execution/ (Order Management)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| order_router.py | 967 | 4/5 | 5/5 | 4/5 | 4/5 | 4/5 | 4.2/5 |
| position_manager.py | 836 | 5/5 | 4/5 | 4/5 | 5/5 | 5/5 | 4.6/5 |
| fill_tracker.py | 489 | 4/5 | 4/5 | 4/5 | 3/5 | 4/5 | 3.8/5 |
| order_builder.py | 220 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |

### src/risk/ (Risk Management)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| risk_engine.py | 501 | 5/5 | 5/5 | 4/5 | 5/5 | 5/5 | 4.8/5 |
| kelly_sizer.py | 344 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5.0/5 |
| circuit_breaker.py | 256 | 5/5 | 5/5 | 4/5 | 5/5 | 5/5 | 4.8/5 |
| manipulation_detector.py | 258 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |
| portfolio_risk.py | 146 | 4/5 | 3/5 | 3/5 | 4/5 | 4/5 | 3.6/5 |

### src/orchestrator/ (Main Loop)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| lifecycle.py | 652 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |
| scan_cycle.py | 521 | 4/5 | 3/5 | 4/5 | 4/5 | 4/5 | 3.8/5 |
| trade_cycle.py | 339 | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 | 4.0/5 |
| startup.py | 116 | 4/5 | 3/5 | 4/5 | 3/5 | 4/5 | 3.6/5 |

### src/storage/ (Database)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| database.py | 1,743 | 4/5 | 4/5 | 3/5 | 4/5 | 4/5 | 3.8/5 |

### src/dashboard/ (Web UI)

| File | Lines | Quality | Tests | Error Handling | Risk Controls | Docs | Health |
|------|-------|---------|-------|----------------|---------------|------|--------|
| server.py | 210 | 4/5 | 3/5 | 4/5 | 4/5 | 3/5 | 3.6/5 |
| routes_api.py | 182 | 3/5 | 3/5 | 3/5 | 3/5 | 3/5 | 3.0/5 |
| routes_html.py | 151 | 3/5 | 2/5 | 3/5 | 2/5 | 3/5 | 2.6/5 |

---

## Section 1. Structural Integrity

### Directory Tree Summary

```
polyedge/
├── config/                     (settings.yaml, categories.yaml, .env, kalshi_private_key.pem)
├── src/                        (81 modules, 23,842 lines)
│   ├── core/          (8)      API clients, models, key loader
│   ├── analysis/      (20)     Forecasting, calibration, ensemble
│   ├── data/          (16)     External data sources
│   ├── strategies/    (7)      Trading strategies
│   ├── execution/     (5)      Order routing, position tracking
│   ├── risk/          (6)      Risk engine, Kelly, circuit breaker
│   ├── orchestrator/  (5)      Main loop, lifecycle
│   ├── storage/       (2)      SQLite database
│   ├── dashboard/     (5+)     Flask web UI + templates
│   ├── alerts/        (4)      iMessage, daily reports
│   └── main.py, metrics.py, config.py
├── tests/                      (77 files, 22,980 lines, 1,476 test functions)
├── scripts/                    (backtest_engine.py, run_backtest.py, backfill_markets.py, etc.)
├── data/                       (markets.db, logs/, chroma/)
├── ecosystem.config.js         (pm2 config)
├── requirements.txt            (16 pinned dependencies)
├── pyproject.toml
└── Makefile
```

### Orphaned/Dead Code
- **No orphaned files detected.** All modules are imported by at least one other module or by the orchestrator.
- `src/core/polymarket_client.py` is conditionally used (Polymarket disabled) but not dead — it activates when `polymarket.enabled: true`.

### Config File Validation
- **settings.yaml:** Valid YAML, 101 lines, all values correctly typed.
- **ecosystem.config.js:** Correct pm2 config — autorestart enabled, 15 max restarts, 500MB memory limit, 60s kill timeout. Properly parses .env file for env var injection.
- **requirements.txt:** All 16 dependencies pinned to exact versions (`==`). No unused packages.
- **.gitignore:** Comprehensive — covers .env, *.pem, *.key, *.db, __pycache__, data/, credentials*.

### Dependency Audit

| Package | Version | Used | Notes |
|---------|---------|------|-------|
| kalshi-python | 2.1.4 | Yes | Kalshi SDK |
| anthropic | 0.86.0 | Yes | Claude API |
| httpx | 0.28.1 | Yes | HTTP client (13 references) |
| pyyaml | 6.0.3 | Yes | Config loading |
| pydantic | 2.12.5 | Yes | Data validation |
| python-dotenv | 1.2.2 | Yes | .env loading |
| cryptography | 46.0.5 | Yes | RSA signing (6 references) |
| py-clob-client | 0.34.6 | Yes | Polymarket SDK (conditional) |
| fastapi | 0.135.1 | Yes | Dashboard API |
| uvicorn | 0.42.0 | Yes | ASGI server |
| jinja2 | 3.1.6 | Yes | Templates |
| feedparser | 6.0.12 | Yes | RSS parsing |
| ddgs | 9.11.4 | Yes | DuckDuckGo search |
| websockets | 16.0 | Yes | WebSocket client |
| pytest | 9.0.2 | Yes | Testing |
| pytest-asyncio | 1.3.0 | Yes | Async testing |

All dependencies pinned to exact versions. No known CVEs identified for current versions.

---

## Section 2. Configuration & Environment

### Complete Environment Variable List

| Variable | Purpose | Required | Documented |
|----------|---------|----------|------------|
| `KALSHI_API_KEY_ID` | Kalshi trading credentials | Yes | Yes |
| `KALSHI_PRIVATE_KEY_PATH` | RSA key file path | Yes | Yes |
| `ANTHROPIC_API_KEY` | Claude AI API access | Yes | Yes |
| `SERPER_API_KEY` | News search (Serper.dev) | No | Yes |
| `SEARXNG_URL` | Alternative search backend | No | Yes |
| `FRED_API_KEY` | Federal Reserve economic data | No | Yes |
| `METACULUS_API_TOKEN` | Forecast cross-reference | No | Yes |
| `POLYMARKET_PRIVATE_KEY` | Polymarket wallet key | No | Yes |
| `POLYEDGE_LIVE_ENABLED` | Trading safety gate | Yes | Yes |
| `POLYEDGE_DASHBOARD_KEY` | Dashboard API auth | No | Yes |
| `POLYEDGE_CORS_ORIGINS` | Dashboard CORS config | No | Yes |
| `CONFIRM_NON_US_POLYMARKET` | Polymarket residency gate | No | Yes |

**Result:** 12 env vars, all documented in .env.example. Zero undocumented variables.

### Hardcoded Values Check
- No API keys or secrets hardcoded in source code.
- All API endpoint URLs configured in settings.yaml (not hardcoded to production).
- Demo/production switching available for Kalshi (`use_demo: true/false`).

### Secrets in Git
- .env never committed to git history (verified via `git log`).
- .gitignore covers: .env, config/.env, *.pem, *.key, credentials*.json.
- kalshi_private_key.pem permissions correctly enforced at 0o600 by key_loader.py.

---

## Section 3. Kalshi Integration

### Endpoints Used (12 total)

| Endpoint | Purpose | Auth | Retry | Rate Limit |
|----------|---------|------|-------|------------|
| GET /exchange/status | Health check | None | Yes | Yes |
| GET /markets | List markets | None | Yes | Yes |
| GET /markets/{ticker} | Single market | None | Yes | Yes |
| GET /events | Events with nested markets | None | Yes | Yes |
| GET /markets/{ticker}/orderbook | Order book depth | None | Yes | Yes |
| GET /markets/trades | Trade history | None | Yes | Yes |
| GET /portfolio/balance | Account balance | RSA-PSS | Yes | Yes |
| GET /portfolio/positions | Open positions | RSA-PSS | Yes | Yes |
| POST /portfolio/orders | Create order | RSA-PSS | Yes | Yes |
| DELETE /portfolio/orders/{id} | Cancel order | RSA-PSS | Yes | Yes |
| GET /portfolio/orders/{id} | Get order status | RSA-PSS | Yes | Yes |
| GET /portfolio/orders | List open orders | RSA-PSS | Yes | Yes |

### Authentication
- RSA-PSS signing with millisecond timestamp.
- Key loaded lazily with freshness checking via file mtime.
- Private key file permissions enforced (0o600).
- Signed requests include timestamp to prevent replay attacks.

### Rate Limiting
- Reactive: catches 429 responses, parses Retry-After header.
- Exponential backoff with jitter: `min(10, 2^attempt + random(0,1))`.
- Circuit breaker opens after 5 consecutive 5xx errors (max 600s backoff).
- **Gap:** No proactive throttling (see H-2).

### Monetary Calculations
- **Position P&L:** Uses Decimal in position_manager.py (lines 170-181, 261-270, 322-331). GOOD.
- **Balance fetch:** Uses float division in kalshi_client.py:485. BAD (see C-1).
- **Order builder:** Prices clamped to [0.01, 0.99] with warnings logged. GOOD.
- **Settlement P&L:** Uses Decimal. GOOD.

### Settlement Handling
- Position manager correctly calculates realized P&L on settlement.
- Creates synthetic trade record to preserve history.
- Decimal arithmetic used for settlement calculations.

### Market Status Handling
- Market model has `closed` and `status` fields.
- **Gap:** Status not checked before order submission (see C-3).

---

## Section 4. AI Forecasting Pipeline

### Claude Integration Quality: EXCELLENT

**Prompt Engineering:**
- 7 category-specific templates (Politics, Fed/Macro, Geopolitics, Tech/AI, Culture, News Impact, General).
- Explicit calibration instruction: "If you estimate 70%, that means in 100 similar situations, approximately 70 should resolve YES."
- Market price always included for calibration anchoring.
- Resolution criteria included verbatim in prompts.
- Confidence intervals (90% credible interval) required.
- Base rate anchoring with historical data.

**Superforecaster Decomposition:**
- Compound question detection via regex patterns.
- Three decomposition modes: AND (chained conditional), OR (parallel independence), CONDITIONAL.
- Sub-question assessment: sequential with conditioning for AND/CONDITIONAL, parallel for OR.
- Recombination: P(A) x P(B|A) for AND, 1-(1-P(A))(1-P(B)) for OR.
- Capped at 5 sub-questions (safety).

**Model Selection:**
- Primary: claude-sonnet-4-6 (routine assessments).
- High-stakes: claude-opus-4-6 (positions >$50 or edge >15%).
- Category-specific temperatures: Politics 0.35, Fed/Macro 0.30, Geopolitics 0.45, etc.
- Dual-temperature cross-check: temp_low=0.2, temp_high=0.5; skip if disagreement >22%.

**Response Parsing (4-strategy fallback):**
1. Direct JSON parse.
2. Markdown code block extraction.
3. Brace extraction (first `{` to last `}`).
4. Prose extraction (regex for "probability X.XX" or "X%").
- Validation: context word check, ambiguity detection, clamping to [0.01, 0.99].

**Token Management:**
- Daily soft limit: 500k tokens. Hard limit: 1M tokens.
- Per-model pricing tracked (Sonnet: $3/$15 per million, Opus: $15/$60 per million).
- Budget check before each API call.
- Graceful fallback to market price when budget exceeded.

**Circuit Breaker:**
- Opens after 3 consecutive API failures.
- Duration: 5 minutes.
- Returns market price as fallback during circuit open.

### Second Model Integration
- **Not implemented.** Claude is the only LLM integrated.
- Ensemble logic supports multi-model averaging but only uses Claude + market price + community forecasts (Manifold, Metaculus) as additional "models."
- GPT-4o not integrated.

### Ensemble Logic
- Single-model: Claude 85% + market 15% (configurable).
- CI penalty: reduces Claude weight as confidence interval widens.
- Market efficiency scaling: high-volume/high-liquidity markets increase market weight.
- Multi-model: Brier-score-weighted averaging when consensus sources available.
- Efficiency range: 0.3 (thin market, trust models) to 0.95 (deep market, trust market more).

### Calibration Tracking
- Brier score calculation with time-decay weighting (30-day half-life).
- 10-decile calibration curve.
- Per-category analysis with James-Stein shrinkage (k=15).
- Accuracy gating: Brier >0.30 skips category entirely; Brier 0.20-0.30 raises min edge to 8%.
- Base rate tracking: % YES resolutions per category, injected into prompts.

---

## Section 5. Data Pipeline & News Integration

### Search Backend Strategy
1. **Primary:** DuckDuckGo (free, via `ddgs` library). No API key required.
2. **Fallback:** Serper.dev (paid). Disabled for 1 hour after auth failure.

### Article Processing
- Max 5 results per query, max 4 queries per market.
- Full article text fetched for top 3 results (not just snippets).
- Custom HTML parser strips navigation, scripts, styles.
- Truncated at sentence boundaries to ~4,000 chars.
- Source trust multipliers: Reuters/AP 1.3x, NYT/WSJ 1.2x, default 1.0x.
- Deduplication by URL (10k cache) and cosine similarity (0.7 threshold).

### Data Enrichment Sources
- FRED: Economic indicators (CPI, employment, GDP) with API key sanitization in logs.
- CME FedWatch: Fed rate expectations.
- Cleveland Fed: GDP nowcast.
- Metaculus: Community probability forecasts.
- Manifold Markets: Community probability forecasts.
- RSS feeds: Reuters top news and business news (2 feeds configured).

### Freshness
- RSS poll interval: 120 seconds.
- Breaking news threshold: age <30 minutes, relevance >0.3.
- Market assessments skipped if prediction <24h old AND price moved <10%.

---

## Section 6. Trading Logic & Risk Management

### Decision Engine
- **Edge threshold:** 5% minimum for AI strategy, 2% for arbitrage, 1% for obvious NO.
- **Confidence requirement:** 60% minimum.
- **Divergence gates:** Category-specific (30% for data-rich, 45% for uncertain). Extreme prices capped at 25%.
- **Accuracy gating:** Categories with Brier >0.30 are skipped entirely.

### Position Sizing: Half-Kelly with 7 Adjustment Layers
1. Base Kelly: `f = (p*b - q) / b * kelly_fraction`.
2. Confidence multiplier: `max(0.2, 0.2 + 0.8 * confidence)`.
3. Calibration multiplier: 0.0x (Brier >0.30) to 1.1x (Brier <0.10).
4. Edge multiplier: 0.3x to 1.0x (corrects systematic edge overestimation).
5. Regime multiplier: 0.0x to 1.2x (market volatility adjustment).
6. Liquidity adjustment: halves if >10% of book, 75% if >5%.
7. Three-layer cap: 5% position, 40% total, 20% correlated.

### Risk Engine: 16-Point Pre-Trade Check
| # | Check | Threshold |
|---|-------|-----------|
| 0 | Excluded category | Crypto, Sports blocked |
| 1 | Balance | Includes pending orders |
| 2 | Position size | 5% of bankroll |
| 3 | Total exposure | 40% of bankroll |
| 4 | Correlated exposure | 20% by event |
| 5 | Circuit breaker | Halt state |
| 6 | Liquidity | Order < 10% of book |
| 7 | Existing position | Warn on additions |
| 8a-8e | Signal quality | Confidence, cost, edge, probability, edge<probability |
| 9 | Resolution date | Reject <4h, warn <1 day |
| 10 | Cooldown | 4h after loss, 1h after profit |
| 11 | Wash trade | 30 min block after exit |
| 12 | Manipulation | Rapid moves, crossed book |
| 13 | Obvious NO limit | 10% bankroll cap |
| 14 | Max concurrent | Hard cap on positions |
| 15 | Spread vs edge | Reject if spread > 50% of edge |

### Exit Triggers (5 independent)
1. **Stop-loss:** 20% loss (with 2% slippage buffer).
2. **Trailing stop:** Trail 35% of peak gain (activates after 12% gain).
3. **Take-profit:** 80% of theoretical max gain.
4. **Max hold:** 21 days.
5. **Edge-gone:** <20% of original edge remaining.

All exit checks require price freshness <2 minutes.

### Circuit Breaker (4 triggers)
1. Max drawdown: 20% peak-to-trough.
2. Unrealized loss gate: 15% of bankroll.
3. Daily loss limit: 10% of bankroll (unrealized discounted at 75%).
4. Consecutive losing days: 3-4 = reduced sizing (0.5x), 5+ = full halt.

State persisted to database, survives restarts, auto-resets after 24 hours.

---

## Section 7. Backtesting & Performance Tracking

### Backtest Infrastructure
- **Strategy Replay Engine** (scripts/backtest_engine.py, 1,127 lines): Replays strategies against historical snapshots with real RiskEngine/KellySizer/CircuitBreaker. Degradation factors applied (AI=0.65, Obvious NO=0.85). Acknowledges limitations (lookahead bias flagged with `uses_lookahead=True`).
- **Claude Calibration Backtest** (src/scripts/backtest.py): Fetches settled Kalshi markets, runs blind Claude forecast, scores against actual outcomes.
- **Test coverage:** 40 test cases across backtest modules.

### Calibration Tracking
- Brier score: Time-decay weighted (30-day half-life), per-category with James-Stein shrinkage.
- Calibration curve: 10 deciles with avg_predicted vs avg_actual.
- Performance by category: Bias detection (over/underconfidence).
- Edge-vs-actual tracking: Measures whether detected edges produce actual returns.
- Edge/return log bounded to 1,000 entries in metrics.py.

### Trade Logging
- All signals logged with: market_id, predicted_probability, market_price, strategy, timestamp.
- All trades logged with: entry price, exit price, P&L, model used, edge at entry.
- Resolution tracking: predicted vs actual outcome for Brier score calculation.
- Daily P&L aggregation for circuit breaker and reporting.

---

## Section 8. Error Handling & Reliability

### Exception Handling
- **1 silent catch:** `src/storage/database.py:1528` — `except Exception: return None` (see M-1).
- **2 rollback catches:** `src/execution/fill_tracker.py:278, 360` — catch, rollback, re-raise (acceptable).
- **0 bare except clauses** (all specify exception type).
- **0 mutable default arguments.**

### Retry Logic Coverage
| API | Retries | Strategy |
|-----|---------|----------|
| Kalshi REST | 3 | Exponential backoff with Retry-After parsing |
| Claude API | 3 | Exponential backoff + circuit breaker (5 min) |
| Serper | 2 | Exponential backoff with auth failure detection |
| FRED/Metaculus | 3 | Timeout + connect error handling |
| Polymarket | None | No explicit retry (see M-2) |

### Graceful Degradation
- **Anthropic down:** Circuit breaker opens after 3 failures; falls back to market price.
- **Kalshi down:** Circuit breaker opens after 5 consecutive 5xx; no trades executed.
- **Serper down:** Falls back to DuckDuckGo; news context may be thinner.
- **Internet drop mid-trade:** Order timeout (60s); pending order cleanup runs periodically (24h stale threshold).

### State Persistence
- Open positions tracked in SQLite. On restart, positions are loaded from DB.
- Circuit breaker state persisted (halted, consecutive losing days, halt time).
- Bankroll synced to DB for crash recovery.
- PID file prevents duplicate instances.

### Memory Management
- Edge/return log bounded to 1,000 entries.
- API latency deques bounded to 100 per metric.
- Processed fills set bounded to 10,000.
- Pending orders cleaned after 24h.
- HTTP clients use context managers (properly closed).
- WebSocket connection closed in `close()` method.

### pm2 Recovery
- `autorestart: true` with `max_restarts: 15`.
- `min_uptime: "10s"` prevents crash loops.
- `restart_delay: 10000` (10s between restarts).
- `kill_timeout: 60000` (60s graceful shutdown).
- `max_memory_restart: "500M"` prevents OOM.

---

## Section 9. Security Review

### Credential Exposure
- **No hardcoded secrets** in source code. Verified by searching for patterns: sk-ant, api_key, secret, password, token.
- **API keys stored in .env** (excluded from git via .gitignore).
- **Private key file** at 0o600 permissions, enforced by key_loader.py with auto-fix and warning if permissive.
- **FRED API key sanitized** in log output (`_sanitize_url()` in fred_client.py).
- **No secrets in git history** (verified via `git log`).
- **No secrets logged** — error messages reference env var names without exposing values.

### .gitignore Coverage
```
.env, config/.env, *.pem, *.key, config/kalshi_private_key*,
credentials*.json, data/, *.db, __pycache__/
```
Status: Comprehensive.

### HTTPS Enforcement
- All production API endpoints use HTTPS.
- WebSocket uses WSS (secure).
- Dashboard runs on localhost HTTP only — acceptable for local use, with explicit warning logged.

### Dashboard Authentication
- API key auth with timing-safe comparison (`hmac.compare_digest()`).
- Without API key: restricted to localhost only (127.0.0.1, ::1).
- CORS: localhost only by default, configurable via env var.
- Methods: GET only (read-safe).

### Command Injection
- No subprocess calls found in production code.
- No user-supplied input passed to shell commands.

---

## Section 10. Code Quality

### Functions Over 50 Lines
26 files exceed 300 lines total. The largest are database.py (1,743), claude_forecaster.py (1,003), and order_router.py (967). Most are well-structured with clear sub-sections and inline documentation.

### TODO/FIXME Comments
1. `src/data/whale_monitor.py:183` — TODO: Refactor `_log_whale_trade()` to use Database abstraction layer.

Only 1 TODO found. Zero FIXME/HACK/XXX markers.

### Code Quality Positives
- Zero bare except clauses.
- Zero mutable default arguments.
- Zero print() statements (all structured logging via `logging` module).
- Well-organized imports (PEP 8 compliant: stdlib, third-party, local).
- Extensive inline comments with reference tags (H-X, L-X, M-X for audit trail).
- All critical data structures are Pydantic models with validation.
- f-strings used consistently throughout.

### Code Quality Concerns
- Type hint coverage at ~61% (see M-8).
- Retry logic duplicated across 3 files (see M-3).
- 3 files exceed 900 lines (could benefit from splitting, see M-7).
- One hardcoded magic number for model selection (see L-2).

---

## Section 11. Regulatory Compliance

### Exchange Targeting
- **Primary exchange:** Kalshi (CFTC-regulated, legal for US users). COMPLIANT.
- **Secondary exchange:** Polymarket. Disabled by default (`polymarket.enabled: false`).

### Polymarket Safeguards (3 independent gates)
1. **Config gate:** `polymarket.enabled: false` in settings.yaml.
2. **Environment gate:** `POLYMARKET_PRIVATE_KEY` must be set in .env.
3. **Residency gate:** `CONFIRM_NON_US_POLYMARKET=true` must be set. Checked at `src/execution/order_router.py:242` and `:607`. If not set, Polymarket live orders are BLOCKED with explicit error message directing user to confirm non-US residency.

**Assessment:** The system does NOT trade on Polymarket by default. Three independent gates must be explicitly unlocked. The residency confirmation mechanism is appropriate. No Polymarket trades can execute without deliberate opt-in.

### Category Restrictions
- Crypto and Sports markets excluded at TWO levels: scanner filtering AND risk engine check 0.
- Case-insensitive bidirectional matching prevents bypass via category naming variants.

### Position Limits
- Max 5% per position, 40% total, 20% correlated — well within reasonable limits.
- No attempt to circumvent Kalshi position limits.

### Market Manipulation Prevention
- Wash trade detection (30-min cooldown after exit).
- Manipulation detector flags rapid price moves (>20% per snapshot), slow drift (4+ monotonic snapshots, >15% cumulative), and crossed books (>5% deviation from sum=1.0).
- All manipulation checks run before order submission.

### Record Keeping
- All trades logged to SQLite with: timestamp, market, side, price, size, P&L.
- All signals logged with: strategy, predicted probability, edge, reasoning.
- Daily P&L summaries generated.
- Sufficient for tax reporting purposes.

---

## Section 12. Improvement Roadmap Audit

| Feature | Status | Notes |
|---------|--------|-------|
| Feeding Kalshi market price into Claude's prompt | IMPLEMENTED | Present in all 7 prompt templates |
| GPT-4o as second forecaster for ensemble averaging | NOT IMPLEMENTED | Claude-only; ensemble uses community forecasts instead |
| Superforecaster-style prompt decomposition | IMPLEMENTED | AND/OR/CONDITIONAL with chained conditioning |
| Fetching full article text from Serper results | IMPLEMENTED | Top 3 articles fetched with custom HTML parser |
| Multi-model ensemble with disagreement handling | PARTIALLY IMPLEMENTED | Multi-source (Manifold, Metaculus, Polymarket cross-ref) but no second LLM |
| Calibration tracking with Brier scores | IMPLEMENTED | Time-decay weighted, per-category with James-Stein shrinkage |
| Performance dashboard | IMPLEMENTED | Flask dashboard with portfolio, strategy, calibration views |

---

## Top 10 Recommendations (Prioritized)

### 1. Fix Float Balance Handling (C-1)
**Risk reduction.** Use Decimal for Kalshi balance conversion. This is the only place in the money pipeline using float — everything downstream uses Decimal. Fix the source.

### 2. Verify Kalshi Side/Action Parameter Mapping (C-2)
**Risk reduction.** Add integration test against Kalshi demo API for all 4 side/action combinations. Wrong-side orders are the highest-impact single bug possible.

### 3. Add Market Status Check Before Order Submission (C-3)
**Reliability.** Simple guard that prevents wasted API calls and state corruption from submitting to closed markets.

### 4. Make Balance Pre-Flight Blocking for Large Orders (H-1)
**Risk reduction.** For orders >10% of bankroll, don't skip the balance check on timeout.

### 5. Implement Proactive Rate Limiting (H-2)
**Reliability.** Token bucket rate limiter prevents thundering herd on 429s. Especially important during high-volume scan cycles.

### 6. Fix Price Comparison in Order Reconciliation (H-3)
**Risk reduction.** Integer cents comparison prevents false positive fill matching on low-priced markets.

### 7. Add WebSocket Connection Lock (H-4)
**Reliability.** Protect `_ws` state with asyncio.Lock() to prevent rare race during reconnection.

### 8. Add Integration Test for Full Risk Pipeline (M-9)
**Reliability.** Single test exercising signal -> Kelly -> risk engine -> circuit breaker catches interaction bugs.

### 9. Extract Retry Logic Helper (M-3)
**Code quality.** DRY out the retry pattern from 3 files into a shared utility.

### 10. Add GPT-4o as Second Forecaster (Roadmap)
**Performance.** The ensemble infrastructure exists and supports multi-model averaging with Brier-score weighting. Adding a second LLM would enable true multi-model disagreement detection and improve calibration. The system is architecturally ready — it just needs the OpenAI SDK integration.

---

## Conclusion

PolyEdge is a well-engineered trading system with comprehensive risk management, strong test coverage (1,476 tests, 96% test-to-code ratio), and production-grade error handling. The AI forecasting pipeline is particularly impressive — category-specific prompts, superforecaster decomposition, dual-temperature cross-checking, and Brier-score-calibrated gating represent best-in-class LLM-driven prediction market analysis.

**Critical items requiring immediate attention:** 3 (float balance, side/action verification, market status check).
**High items for this week:** 7.
**Medium items for next sprint:** 10.
**Low/optional:** 4.

The system is safe to operate in paper trading mode. Before transitioning to live trading, address all Critical and High items. The risk management architecture (16-point pre-trade check, 4-trigger circuit breaker, 5-exit-trigger position management, Half-Kelly with 7 adjustment layers) is robust and well-tested.

---

*Report generated by Claude Code on March 31, 2026. All file paths and line numbers verified against commit f8a71d2.*
