# PolyEdge — Complete Codebase Audit Report

**Audit Date:** April 4, 2026
**Auditor:** Claude Opus 4.6 (automated, 6 parallel audit agents)
**Codebase:** `/Users/adamgrodin/polyedge` (branch: main, commit: d7237a2)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Source files** | 103 (.py in src/ + scripts/, excl. __init__.py) |
| **Test files** | 101 (test_*.py) |
| **Total lines of code** | 31,055 (source) + 36,303 (tests) = ~67,358 |
| **External API integrations** | 7 (Kalshi REST, Kalshi WS, Anthropic, Serper, FRED, Metaculus, DuckDuckGo) |
| **Trading mode** | Paper (POLYEDGE_LIVE_ENABLED=false) |
| **Environment variables** | 12 total (6 required, 6 optional) — all documented in .env.example |
| **Dependencies** | 17 direct (all pinned to exact versions) |
| **Test coverage** | ~95% of modules have corresponding tests |

---

## Issues by Severity

### CRITICAL (5 issues) — FIX BEFORE NEXT TRADE

**C-1. Order Creation Timeout with Orphaned Order Risk**
- **File:** `src/execution/router_kalshi.py:197-212`
- **What:** When order creation times out (15s), reconciliation is attempted via `get_open_orders()` with 10s timeout. If reconciliation ALSO times out, order status remains unknown. Code returns OrderStatus.OPEN with "unconfirmed" note but doesn't prevent duplicate order placement on retry.
- **Impact:** Real-money loss from duplicate orders. Orphaned orders may execute on Kalshi while bot places a new order. Potential double-exposure on a single signal.
- **Fix:** Increase create_order timeout to 20-30s. After timeout, loop reconciliation up to 3 times with 5s waits. Mark order as PENDING_VERIFICATION with mandatory cooldown before any further action on same market.

**C-2. No Position-Level Stop Losses**
- **File:** Entire codebase (missing feature)
- **What:** No per-position stop loss levels, trailing stops, or profit-taking mechanisms exist. Downside protection relies entirely on the portfolio-level daily loss circuit breaker.
- **Impact:** Individual underwater positions can bleed for days/weeks without intervention. A single bad position could consume a significant portion of the daily loss limit before the circuit breaker triggers.
- **Fix:** Implement position-level stop losses in `position_manager.py:should_exit()`. Add configurable max loss per position (e.g., -30% of entry cost). Track peak P&L per position for optional trailing stop.

**C-3. Partial Fill Handling Missing**
- **File:** `src/execution/fill_tracker.py:189-293`, `src/execution/order_router.py`
- **What:** No explicit strategy for handling partial fills on limit orders. If bot places 100-contract order and only 30 fill, the remaining 70 may go untracked. Position manager and correlation detector may use intended size rather than actual filled size.
- **Impact:** Hidden over/under-exposure. P&L tracking on partially-filled positions may be incorrect. Kelly sizer may over-allocate on next cycle thinking less capital is deployed.
- **Fix:** Track filled vs. placed size separately. After partial fill detected, decide: resubmit remainder at same/better price, or accept partial as final. Update correlation exposure based on actual fill size.

**C-4. Pending Exit Orders Not Persisted Across Restarts**
- **File:** `src/execution/position_manager.py` (missing feature)
- **What:** If the bot crashes with resting exit orders on Kalshi, restart won't see them as pending. This risks orphaned positions where the exit order fills on Kalshi but the bot doesn't know about it.
- **Impact:** Ghost positions in local state. Bot may attempt to re-exit an already-closed position, or fail to recognize freed capital.
- **Fix:** Add `pending_exit_orders` table to database. Persist resting exits on creation, reconcile on startup via `sync_with_kalshi()`.

**C-5. Real API Keys on Disk in Plaintext**
- **File:** `config/.env` (7 API keys/secrets)
- **What:** File contains real Kalshi API key, Anthropic API key (sk-ant-...), Polymarket private key, Serper key, and Metaculus token in plaintext. File permissions are 600 and it's in .gitignore (NOT tracked in git), but any local compromise exposes all credentials.
- **Impact:** Full account takeover on all integrated platforms if Mac Mini is compromised.
- **Fix:** Rotate ALL keys immediately (assume potential exposure from this audit). Consider macOS Keychain integration or a secrets manager for production. At minimum, ensure FileVault is enabled on the disk.

---

### HIGH (12 issues) — FIX THIS WEEK

**H-1. Auth Retry Limited to Single Attempt on 401/403**
- **File:** `src/core/kalshi_client.py:299-306`
- **What:** Only retries once on 401/403 with 2-second delay. No distinction between transient auth glitches (clock skew) and permanent auth failure (expired key).
- **Impact:** Real-world transient auth errors cause order failures even though retry could succeed.
- **Fix:** Implement exponential backoff (2s, 4s, 8s) up to 3 retries with jitter for 401/403.

**H-2. Partial Fill Double-Recording Vulnerability**
- **File:** `src/execution/fill_tracker.py:189-293`
- **What:** When a partial fill is detected, code records it immediately without confirming remaining_count > 0. No idempotency key prevents re-recording the same partial.
- **Impact:** Double-counting of filled contracts, position size overstatement.
- **Fix:** Require remaining_count > 0 before recording partial; add idempotency key to prevent re-recording.

**H-3. Race Between WebSocket Fill and Price Update**
- **File:** `src/core/websocket_client.py:122-123`
- **What:** Fill event may arrive BEFORE corresponding ticker price update. No buffering or ordering between channels.
- **Impact:** P&L calculated using stale prices immediately after fill detection.
- **Fix:** In FillTracker.handle_ws_fill(), use fill price from order object rather than latest ticker price.

**H-4. Calibration Resolution Not Triggered Automatically**
- **File:** `src/analysis/calibration.py` (entire file)
- **What:** `resolve_prediction()` must be called manually after market resolves. No automatic trigger from WebSocket lifecycle events or REST market status checks.
- **Impact:** Markets remain unresolved in DB indefinitely, skewing Brier scores and degrading Kelly calibration multiplier accuracy.
- **Fix:** Call `resolve_prediction()` in market status sync after detecting settled market. Add to lifecycle event handler in fill_tracker or websocket_client.

**H-5. Failed API Retries Consume Token Budget**
- **File:** `src/analysis/claude_forecaster.py:356-376`
- **What:** When rate-limited or connection-error retry happens, code estimates tokens (1500 per retry) and adds them to `_total_tokens_today` even though the API didn't return usage.
- **Impact:** Budget exhausted faster than actual cost. Could halt AI forecasting prematurely.
- **Fix:** Only count actual tokens from response.usage; don't estimate failures. Track retry count separately and alert if >5% of calls are retrying.

**H-6. Missing JSON Schema Validation in Forecast Parser**
- **File:** `src/analysis/forecast_parser.py:139-196`
- **What:** Parser checks for "probability" key but no validation that confidence_low/high or key_factors exist. Falls back to +-0.20 around probability silently.
- **Impact:** Missing confidence intervals may not reflect Claude's intent, leading to incorrect ensemble weighting and Kelly sizing.
- **Fix:** Log WARNING when confidence fields missing. In ensemble, widen CI when fields are absent.

**H-7. Timeout Opens Circuit Breaker Prematurely**
- **File:** `src/analysis/claude_forecaster.py:537-548`
- **What:** When timeout occurs, `_record_api_failure()` increments consecutive_failures. Circuit breaker opens after 3 timeouts even if API is functioning (just slow).
- **Impact:** Circuit breaker too sensitive to network latency. Three slow responses halt all AI forecasting.
- **Fix:** Distinguish read-timeout (transient, don't count as hard failure) from connection-refused (hard failure).

**H-8. Market Efficiency Parameter Unused in Ensemble**
- **File:** `src/analysis/ensemble.py:99-182`
- **What:** `ensemble_forecast()` accepts `market_efficiency` parameter but only uses it to set a default (0.7). Doesn't adapt Claude/market weight ratio based on actual liquidity.
- **Impact:** Market price weighting doesn't adapt to thin vs. deep markets. Low-liquidity markets get same trust as high-liquidity.
- **Fix:** Scale market_weight by market_efficiency: `market_weight = (1 - claude_weight) * market_efficiency`.

**H-9. Scraping-Based Data Sources Fragile (FedWatch, Cleveland Fed)**
- **File:** `src/data/fedwatch.py:108-120`, `src/data/cleveland_fed.py:109-145`
- **What:** Both use regex on HTML pages. Site redesign breaks parsing silently (falls back to stale cache).
- **Impact:** Stale/missing economic data fed to Claude for Fed/Macro category forecasts. Could produce systematically wrong signals.
- **Fix:** Add FRED API fallback for CPI data. Monitor parse success rate. Alert when cache fallback used >3 consecutive times.

**H-10. No Forecast Reasonableness Validation Post-Ensemble**
- **File:** `src/strategies/ai_probability.py` (post-ensemble stage)
- **What:** After parse and ensemble, no sanity check that final probability is finite and in [0.01, 0.99].
- **Impact:** NaN, Inf, or nonsensical probability could propagate to Kelly sizer and order builder.
- **Fix:** Add: `if not (0.01 <= final_prob <= 0.99) or not math.isfinite(final_prob): return None`

**H-11. Missing Tests for Critical Modules**
- **Files:** `src/analysis/prompt_builder.py`, `src/analysis/news_fetcher.py`, `src/execution/router_polymarket.py`, `src/execution/router_paper.py`
- **What:** Four modules with real-money impact (prompt construction, article extraction, live order execution, paper fill simulation) have zero test coverage.
- **Impact:** Regressions in these modules would not be caught.
- **Fix:** Write tests for: prompt_builder (market price injection, template rendering), news_fetcher (HTML parsing, quality filters), router_polymarket (residency gate), router_paper (fill simulation).

**H-12. Kalshi Pointing to Production API in Paper Mode**
- **File:** `config/settings.yaml:4`
- **What:** `use_demo: false` means paper trading still hits the production API (just doesn't submit orders). Rate limits are shared with any future live trading.
- **Impact:** Paper trading consumes production API rate limits. Testing during high-volume periods could cause rate limiting when going live.
- **Fix:** Set `use_demo: true` during development/testing. Reserve production API for paper+live validation.

---

### MEDIUM (15 issues) — FIX WHEN POSSIBLE

**M-1. Order Cost Rounding Uses Float Not Decimal**
- **File:** `src/execution/order_router.py:209`
- **What:** `order.cost = round(order.price * order.size + fee_dollars, 4)` uses float math instead of Decimal.
- **Impact:** Rounding errors on large positions. Cumulative drift at >10K trades.
- **Fix:** Use `Decimal(str(price)) * Decimal(str(size))` and quantize.

**M-2. Database Unencrypted at Rest**
- **File:** `src/storage/database.py`
- **What:** SQLite database stores trade history, P&L, calibration data, whale tracking in plaintext on disk.
- **Impact:** Readable if disk is accessed by attacker.
- **Fix:** Enable FileVault on macOS (system-level) or migrate to SQLCipher.

**M-3. Too-Broad Exception Handlers (200+ instances)**
- **File:** `src/orchestrator/lifecycle.py` (32), `src/orchestrator/scan_cycle.py` (25), `src/strategies/ai_probability.py` (22), others
- **What:** Most exception handling uses `except Exception as e:` rather than specific types. While all are logged, this masks programming errors (TypeError, AttributeError) that should crash loudly.
- **Impact:** Bugs hidden by catch-all handlers. Subtle issues may persist undetected.
- **Fix:** Replace with specific exceptions in innermost handlers. Keep broad catches only at orchestrator boundary.

**M-4. Market Price Divergence Checked Too Late**
- **File:** `src/analysis/claude_forecaster.py:467-474`
- **What:** After forecast, divergence from market price is checked. But this check is downstream of ensemble weighting — by then, high divergence may have already influenced Kelly sizer.
- **Fix:** Check divergence immediately after parse, before ensemble.

**M-5. Extreme Price Adjustment Threshold Too Aggressive**
- **File:** `src/analysis/ensemble.py:143-146`
- **What:** For prices <5c or >95c, Claude weight reduced with `max(0.25, ...)`. But 5c markets still have tradeable volume.
- **Fix:** Change threshold to <1c or >99c.

**M-6. Cache Cleanup Only at 500 Entries**
- **File:** `src/data/cache.py:44`
- **What:** TTL cache cleanup only triggers when >500 entries. Could grow unbounded during high-activity periods.
- **Fix:** Reduce threshold or add periodic background cleanup.

**M-7. Confidence Interval Semantics Unclear in Prompts**
- **File:** `src/analysis/prompt_templates.py:32`
- **What:** Says "90% credible interval" but doesn't explain how to set width. Claude may produce overconfident intervals.
- **Fix:** Add examples: "resolving in 3 days: CI width ~0.05; resolving in 90 days: CI width ~0.20."

**M-8. Model Selection Doesn't Account for Category**
- **File:** `src/analysis/claude_forecaster.py:115-119`
- **What:** `edge_highstakes_threshold` (0.15) is global. Politics (data-rich) triggers Opus at same edge as Geopolitics (data-sparse).
- **Fix:** Use category-specific thresholds.

**M-9. News API Retry Logic Not Visible**
- **File:** `src/analysis/news_researcher.py`
- **What:** Serper retry logic unclear. If news enrichment fails entirely, fallback path is cached context with staleness warning — acceptable but not robust.
- **Fix:** Add explicit retry_with_backoff for Serper calls. Log retry attempts.

**M-10. Backtest Fill Simulation Heuristic, Not Data-Driven**
- **File:** `scripts/backtest_engine.py:571-579`
- **What:** Miss rate (15%) and partial fill (25% chance, 40-80% filled) are arbitrary constants, not calibrated to actual Kalshi order book data.
- **Impact:** Backtest P&L may overstate/understate realistic performance.
- **Fix:** Calibrate against historical fill rates from actual trades.

**M-11. No Probability Bucket Analysis in Backtest**
- **File:** `scripts/backtest_engine.py`
- **What:** Category-level metrics exist but no breakdown by probability bucket (0.5-0.55, 0.55-0.60, etc.).
- **Fix:** Add decile-based calibration visualization to backtest output.

**M-12. Stale Price Detection Doesn't Account for Resolved Markets**
- **File:** `src/execution/position_manager.py:232-255`
- **What:** Flags price as stale if unchanged for 300+ seconds. But resolved markets legitimately show 0.0 or 1.0 indefinitely.
- **Fix:** Skip staleness check for resolved/settled markets.

**M-13. Decomposer Doesn't Inherit Retry Logic**
- **File:** `src/analysis/decomposer.py`
- **What:** `_decompose_question()` and `_assess_sub_questions()` call Claude internally but don't inherit parent's retry_with_backoff.
- **Fix:** Pass retry helper to decomposer or wrap decomposer calls in retry.

**M-14. Calibration Records Grow Indefinitely**
- **File:** `src/storage/database.py`
- **What:** No automatic cleanup for calibration records. Table can grow without bound.
- **Fix:** Add age-based cleanup (>1 year) or partition by date.

**M-15. Dashboard Authentication Optional**
- **File:** `src/dashboard/server.py:87-89`
- **What:** If `POLYEDGE_DASHBOARD_KEY` not set, dashboard is unauthenticated on local network.
- **Fix:** Always set `POLYEDGE_DASHBOARD_KEY` in production. Warn at startup if missing.

---

### LOW (8 issues) — OPTIONAL

**L-1.** Missing return type hints on `run_trading_loop()`, `main()`, `update_price()` (`src/orchestrator/lifecycle.py:56,697`, `src/execution/position_manager.py:216`)

**L-2.** DuckDuckGo search is unofficial and could be rate-limited without warning (`src/analysis/news_search.py`)

**L-3.** Log rotation via pm2-logrotate is commented out as instructions only (`ecosystem.config.js:57-61`)

**L-4.** 163 functions over 50 lines — legitimate complexity but candidates for future refactoring (top offenders: `database._run_migrations` 287 lines, `scan_cycle.scan_and_trade` 269 lines)

**L-5.** Metaculus API no longer returns community predictions (`src/data/metaculus_client.py:110-114`) — gracefully disabled but dead code remains

**L-6.** No API version detection — if Kalshi/Polymarket ships breaking v3 API, bot won't warn (`src/core/kalshi_client.py`)

**L-7.** Time-decay half-life for Brier scores hardcoded to 30 days (`src/analysis/calibration.py:136`)

**L-8.** Extremization factor (1.15) in ensemble not validated for edge probabilities near 0/1 (`src/analysis/ensemble.py:29-47`)

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS + key reload | 429/401/5xx/timeout | Exp backoff + circuit breaker | Token bucket (8/s) | 30s (httpx) | 1010 LOC | **Solid** |
| Kalshi WebSocket | RSA-PSS headers | Reconnect + lifecycle msgs | Auto-reconnect (1-60s backoff) | N/A | ping 20s/timeout 30s | 527 LOC | **Solid** |
| Anthropic (Claude) | API key env var | Rate limit/connection/timeout | retry_with_backoff + budget | Pre-call budget check | Configurable (default 60s) | 401 LOC | **Good** |
| Serper (Search) | API key header | 3-strike auth disable | Auto-recovery (1h probe) | Implicit | 8s (DDG fallback) | 567 LOC (news) | **Good** |
| FRED | API key query param | Cache fallback | Retry on timeout | N/A | httpx default | 175 LOC | **Good** |
| Metaculus | Bearer token | Auto-disable on failure | N/A | N/A | httpx default | 211 LOC | **Fair** |
| DuckDuckGo | None (public) | Timeout + empty fallback | N/A | N/A | 8s executor | Indirect | **Fair** |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi + Polymarket scanners, category filtering, volume/liquidity thresholds | 148+142 LOC | Exclude categories, min volume/liquidity | **Solid** |
| Forecast Generation | Claude Sonnet/Opus, decomposition, cross-check, prompt templates per category | 401+553 LOC | Budget limits, circuit breaker, timeout fallback | **Solid** |
| Edge Detection | Forecast vs. market price, per-strategy min edge, impossible-edge guard | Via ai_probability tests | Divergence gates, confidence gating, cross-check | **Solid** |
| Position Sizing | Half/Quarter-Kelly, Brier calibration multiplier, liquidity adjustment, fee-aware | 542 LOC | Price viability, max position %, correlation caps | **Excellent** |
| Order Execution | Kalshi router, paper router, order builder with Decimal arithmetic | 157+580+1273 LOC | Balance check, market validation, side mapping | **Good** |
| Position Tracking | Weighted avg entry, proportional fees, peak P&L, sync with Kalshi | 580 LOC | Stale price detection, exit rules | **Good** |
| P&L Calculation | Realized + unrealized, fee deduction, high water mark | Via circuit breaker tests | Daily loss limit, drawdown protection | **Good** |
| Settlement Handling | WebSocket lifecycle events, binary validation, settlement value check | 527 LOC | Market status validation before orders | **Good** |

---

## Module-by-Module Scorecard

### Core (src/core/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| kalshi_client.py | 655 | 4/5 | 5/5 | 4/5 | 4/5 | **4/5** |
| websocket_client.py | 600 | 4/5 | 4/5 | 4/5 | 3/5 | **4/5** |
| models.py | 537 | 5/5 | 4/5 | N/A | N/A | **5/5** |
| market_discovery.py | 351 | 4/5 | 4/5 | 4/5 | 3/5 | **4/5** |
| polymarket_discovery.py | 321 | 3/5 | 3/5 | 3/5 | 3/5 | **3/5** |
| polymarket_client.py | 243 | 3/5 | 4/5 | 3/5 | 3/5 | **3/5** |
| key_loader.py | 83 | 5/5 | 5/5 | 4/5 | 4/5 | **5/5** |
| retry_helper.py | 69 | 5/5 | 5/5 | 5/5 | N/A | **5/5** |

### Analysis (src/analysis/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| claude_forecaster.py | 822 | 4/5 | 4/5 | 4/5 | 4/5 | **4/5** |
| prompt_templates.py | 436 | 4/5 | 4/5 | N/A | 3/5 | **4/5** |
| decomposer.py | 422 | 4/5 | 5/5 | 3/5 | 3/5 | **4/5** |
| prompt_ab_testing.py | 417 | 3/5 | 4/5 | 3/5 | N/A | **3/5** |
| calibration_analyzer.py | 388 | 4/5 | 4/5 | 3/5 | N/A | **4/5** |
| calibration.py | 379 | 4/5 | 4/5 | 3/5 | 3/5 | **4/5** |
| ensemble.py | 371 | 4/5 | 5/5 | 3/5 | 3/5 | **4/5** |
| news_researcher.py | 600 | 4/5 | 5/5 | 4/5 | 3/5 | **4/5** |
| forecast_parser.py | 196 | 3/5 | 4/5 | 3/5 | 2/5 | **3/5** |
| prompt_builder.py | 140 | 4/5 | 1/5 | 3/5 | 3/5 | **3/5** |
| news_fetcher.py | 227 | 4/5 | 1/5 | 3/5 | N/A | **3/5** |

### Strategies (src/strategies/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| ai_probability.py | 1092 | 4/5 | 5/5 | 4/5 | 4/5 | **4/5** |
| cross_arb.py | 521 | 4/5 | 5/5 | 3/5 | 4/5 | **4/5** |
| late_resolution.py | 251 | 4/5 | 5/5 | 3/5 | 3/5 | **4/5** |
| cross_platform_arb.py | 236 | 3/5 | 4/5 | 3/5 | 3/5 | **3/5** |
| mean_reversion.py | 198 | 4/5 | 4/5 | 3/5 | 4/5 | **4/5** |
| whale_tracker.py | 193 | 3/5 | 4/5 | 3/5 | 3/5 | **3/5** |
| news_reactive.py | 171 | 3/5 | 3/5 | 3/5 | 3/5 | **3/5** |
| obvious_no.py | 133 | 4/5 | 4/5 | 3/5 | 4/5 | **4/5** |

### Execution (src/execution/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| router_kalshi.py | 512 | 4/5 | 4/5 | 4/5 | 4/5 | **4/5** |
| fill_tracker.py | 499 | 3/5 | 5/5 | 3/5 | 3/5 | **3/5** |
| order_router.py | 485 | 4/5 | 5/5 | 4/5 | 4/5 | **4/5** |
| position_manager.py | 865 | 4/5 | 4/5 | 3/5 | 3/5 | **4/5** |
| order_builder.py | 228 | 4/5 | 3/5 | 3/5 | 4/5 | **4/5** |
| router_polymarket.py | 179 | 3/5 | 1/5 | 3/5 | 4/5 | **3/5** |
| router_paper.py | 141 | 3/5 | 1/5 | 3/5 | 3/5 | **3/5** |

### Risk (src/risk/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| kelly_sizer.py | 444 | 5/5 | 5/5 | 4/5 | 5/5 | **5/5** |
| risk_checks.py | 440 | 4/5 | 4/5 | 4/5 | 5/5 | **4/5** |
| circuit_breaker.py | 408 | 4/5 | 5/5 | 4/5 | 5/5 | **5/5** |
| risk_engine.py | 271 | 4/5 | 5/5 | 4/5 | 5/5 | **4/5** |
| manipulation_detector.py | 258 | 4/5 | 4/5 | 3/5 | 4/5 | **4/5** |
| monte_carlo.py | 241 | 4/5 | 4/5 | 3/5 | N/A | **4/5** |
| correlation_detector.py | 226 | 4/5 | 4/5 | 3/5 | 4/5 | **4/5** |
| portfolio_risk.py | 145 | 4/5 | 3/5 | 3/5 | 4/5 | **4/5** |

### Storage (src/storage/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| database.py | 759 | 4/5 | 5/5 | 4/5 | 3/5 | **4/5** |
| db_trades.py | 424 | 4/5 | 5/5 | 4/5 | 3/5 | **4/5** |
| db_markets.py | 266 | 4/5 | 5/5 | 4/5 | 3/5 | **4/5** |
| db_calibration.py | 200 | 4/5 | 5/5 | 3/5 | 3/5 | **4/5** |

### Orchestrator (src/orchestrator/)

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| lifecycle.py | 831 | 4/5 | 5/5 | 5/5 | 4/5 | **4/5** |
| scan_cycle.py | 686 | 4/5 | 5/5 | 4/5 | 4/5 | **4/5** |
| trade_cycle.py | 335 | 4/5 | 4/5 | 4/5 | 4/5 | **4/5** |
| startup.py | 166 | 4/5 | 4/5 | 4/5 | 4/5 | **4/5** |

### Alerts & Dashboard

| Module | LOC | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|-----|---------|-------|----------------|---------------|---------|
| alert_manager.py | 116 | 4/5 | 4/5 | 3/5 | N/A | **4/5** |
| daily_report.py | 145 | 4/5 | 3/5 | 3/5 | N/A | **3/5** |
| dashboard/server.py | 215 | 4/5 | 4/5 | 3/5 | 2/5 | **3/5** |

---

## Regulatory Compliance (Section 11)

| Check | Status | Notes |
|-------|--------|-------|
| Bot targets Kalshi (CFTC-regulated) | **PASS** | Primary platform, legally available to US residents |
| No active Polymarket trading | **PASS** | `polymarket.enabled: false` by default; residency gate requires `CONFIRM_NON_US_POLYMARKET=true` |
| Polymarket usage is data-only | **PASS** | Cross-reference for price comparison, not trading |
| No TOS circumvention | **PASS** | No VPN/proxy bypass; explicit legal warnings in code |
| Position limits enforced | **PASS** | 5% per position, 40% total, 20% correlated |
| No market manipulation | **PASS** | Manipulation detector active; no aggressive order patterns |
| Tax record-keeping | **PASS** | `scripts/export_tax_report.py` (475 LOC): FIFO matching, Schedule D/Form 8949 format, long/short-term classification |

---

## Improvement Roadmap Status (Section 12)

| Feature | Status | Evidence |
|---------|--------|---------|
| Kalshi market price in Claude prompt | **Implemented** | `prompt_templates.py` line 73+: `CURRENT MARKET PRICE: {market_price:.0%}` in all templates |
| GPT-4o as second forecaster | **Not Implemented** | No OpenAI SDK anywhere. Multi-model framework exists in `ensemble.py:185-249` but only Claude used |
| Superforecaster-style decomposition | **Implemented** | `decomposer.py` (422 LOC): AND/OR/CONDITIONAL detection, sub-question assessment, recombination |
| Full article text from Serper results | **Implemented** | `news_fetcher.py` (227 LOC): HTML parsing, 50-word quality filter, 3000-char sentence-boundary truncation |
| Multi-model ensemble with disagreement | **Partial** | Framework ready (`ensemble.py` multi_model_ensemble). Brier-weighted averaging implemented. Only Claude models used. Cross-check disagreement exists for dual-temperature, not cross-model |
| Calibration tracking with Brier scores | **Implemented** | `calibration.py` + `calibration_analyzer.py` + `db_calibration.py`: prediction logging, resolution, Brier computation, category breakdown |
| Performance dashboard | **Implemented** | `dashboard/` (4 files): FastAPI on port 8080, portfolio overview, trade history, calibration view, risk monitoring |

---

## Top 10 Recommendations (Prioritized)

### 1. Implement Position-Level Stop Losses (Risk Reduction)
Individual positions can bleed indefinitely. Add configurable max-loss-per-position exits in `position_manager.py:should_exit()`. This is the single biggest gap in downside protection.

### 2. Fix Order Timeout Reconciliation (Risk Reduction)
C-1: Orphaned orders from timeouts can cause double exposure. Increase timeout, add multi-attempt reconciliation, implement mandatory cooldown per market after timeout.

### 3. Add Partial Fill Tracking (Risk Reduction)
C-3: Track filled vs. placed size separately. Update all downstream calculations (correlation, P&L, Kelly) based on actual fills, not intended size.

### 4. Persist Pending Exit Orders (Reliability)
C-4: Exit orders in flight survive process crashes on Kalshi but not in local state. Add `pending_exit_orders` table and reconcile on startup.

### 5. Rotate All API Keys (Security)
C-5: All keys are now visible in this audit report. Rotate Kalshi, Anthropic, Polymarket, Serper, and Metaculus credentials. Consider macOS Keychain or secrets manager.

### 6. Auto-Resolve Calibration on Market Settlement (Performance)
H-4: Brier scores are the foundation of Kelly sizing adjustment. Unresolved markets degrade calibration accuracy. Wire settlement events to `resolve_prediction()`.

### 7. Fix Circuit Breaker Timeout Sensitivity (Reliability)
H-7: Three network timeouts shouldn't halt all AI forecasting. Distinguish timeout from connection failure.

### 8. Add Tests for Untested Critical Modules (Reliability)
H-11: prompt_builder, news_fetcher, router_polymarket, and router_paper have zero tests. These directly affect signal quality and trade execution.

### 9. Narrow Exception Handlers in Orchestrators (Code Quality)
M-3: 200+ `except Exception` blocks mask real bugs. Replace innermost handlers with specific types; keep broad catches only at module boundaries.

### 10. Add FRED Fallback for Economic Data (Performance)
H-9: FedWatch and Cleveland Fed scrapers are fragile. Add FRED API as backup for CPI/Fed data to ensure Claude always has current economic context.

---

## Architecture Strengths

The codebase demonstrates several strong design patterns:

- **Defense-in-depth risk management**: 10+ risk checks in pipeline, daily/drawdown/unrealized circuit breakers, manipulation detection, correlation limits
- **Kelly sizing excellence**: Brier-calibrated, liquidity-adjusted, fee-aware, with dynamic Kelly fraction based on rolling win rate
- **Graceful degradation**: Every optional component (news, whales, Polymarket, alerts) can fail independently without halting core trading
- **Comprehensive logging**: Dual-format (human + JSON), rotating files, restricted permissions, full decision audit trail
- **Clean async architecture**: Single-threaded event loop eliminates race conditions; token bucket rate limiting prevents API abuse
- **Tax compliance built-in**: FIFO matching, Schedule D format, long/short-term classification from day one
- **Well-pinned dependencies**: All 17 packages at exact versions, no known CVEs

---

**Overall Assessment: Production-viable with targeted fixes needed.**

The codebase is well-engineered for a trading system. Risk management and calibration are particularly strong. The 5 CRITICAL issues (order timeout reconciliation, stop losses, partial fills, exit order persistence, and credential rotation) should be addressed before scaling beyond current paper trading. The 12 HIGH issues should be resolved within the first week of live trading validation. The system's multi-layer safety gates (paper mode, env var gate, config mode) provide adequate protection during the fix period.

**Estimated effort to resolve all CRITICAL + HIGH issues: 3-5 days.**
