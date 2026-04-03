# PolyEdge Re-Audit Report

**Date:** April 3, 2026
**Auditor:** Claude Opus 4.6 (automated, 12-section systematic re-audit)
**Context:** Re-audit after fixing 28 issues from initial audit (March 31, 2026)
**Test Suite:** 2,042 tests, all passing

---

## Summary Dashboard

| Metric | Before (Mar 31) | After (Apr 3) | Change |
|--------|-----------------|---------------|--------|
| Source files | 80 | 101 | +21 |
| Source LOC | 25,620 | 26,135 | +515 |
| Test files | 87 | 101 | +14 |
| Test count | 1,968 | 2,042 | +74 |
| Test LOC | 30,200 | 31,118 | +918 |
| Test-to-code ratio | 1.18x | 1.19x | ↑ |
| Critical issues | 0 | 0 | — |
| High issues | 7 | 2 | -5 |
| Medium issues | 14 | 8 | -6 |
| Low issues | 7 | 5 | -2 |
| Bare except clauses | 0 | 0 | — |
| TODO/FIXME comments | 0 | 0 | — |
| print() statements | 0 | 0 | — |

### External API Integration Health

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | ✅ RSA signing | ✅ Status codes | ✅ Shared retry_helper | ✅ Token bucket | ✅ 30s | 45+ | ✅ |
| Kalshi WebSocket | ✅ Auth header | ✅ Reconnect | ✅ Auto-reconnect | N/A | ✅ | 12 | ✅ |
| Anthropic (Claude) | ✅ API key | ✅ Rate limit/auth | ✅ Shared retry_helper | ✅ Budget gate | ✅ 60s | 42+ | ✅ |
| Serper (Search) | ✅ API key | ✅ Fallback sources | ✅ Shared retry_helper | ✅ | ✅ 15s | 18+ | ✅ |
| CME FedWatch | N/A (public) | ✅ + stale fallback | ✅ Shared retry_helper | N/A | ✅ 15s | 22 | ✅ |
| Cleveland Fed | N/A (public) | ✅ + stale fallback | ✅ Shared retry_helper | N/A | ✅ 15s | 24 | ✅ |
| FRED | ✅ API key | ✅ | ✅ | N/A | ✅ | 8 | ✅ |
| Metaculus | ✅ API token | ✅ | ✅ | N/A | ✅ | 6 | ✅ |

---

## Issues Resolved Since Last Audit

### All 7 High Issues — RESOLVED

| ID | Issue | Fix Applied |
|----|-------|-------------|
| H-1 | FedWatch probability normalization | Added normalization when sum ≠ 100% |
| H-2 | Float arithmetic in order builder | Converted to Decimal with ROUND_HALF_UP |
| H-3 | Race condition: pending order cost not checked | Added `_pending_order_cost` tracking |
| H-4 | Market discovery accepts markets without prices | Added ticker + price validation (Kalshi field names) |
| H-5 | Circuit breaker high water mark lost on restart | Persisted to DB via migration v15 |
| H-6 | Backtest lookahead bias via synthetic forecasts | MockForecaster defaults cached_only=True |
| H-7 | Polymarket legal compliance warning | Added startup warning for US residents |

### All 14 Medium Issues — RESOLVED

| ID | Issue | Status |
|----|-------|--------|
| M-1 | ExecutionConfig missing validators | ✅ FIXED |
| M-2 | Kalshi order param validation | ✅ FIXED |
| M-3 | Cleveland Fed CPI bounds checking | ✅ FIXED |
| M-4 | News URL dedup with tracking params | ✅ FIXED |
| M-5 | Log message time mismatch | ✅ FIXED |
| M-6 | Token tracking on retry | ✅ FIXED |
| M-7 | Failure counter not reset on success | ✅ FIXED |
| M-8 | Unrealized loss weighting comments | ✅ FIXED |
| M-9 | Lock ordering (from prior session) | ✅ FIXED |
| M-10 | Split database.py (1,811 LOC) | ✅ FIXED — 6 domain mixins |
| M-11 | Split order_router.py (1,060 LOC) | ✅ FIXED — 3 platform routers |
| M-12 | Consolidate retry logic | ✅ FIXED — shared retry_helper |
| M-13 | Market price in Claude prompts | ✅ Already implemented |
| M-14 | Tax reporting export | ✅ FIXED — scripts/export_tax_report.py (27 tests) |

### All 7 Low Issues — RESOLVED

| ID | Issue | Status |
|----|-------|--------|
| L-1 | Unused enums in models.py | ✅ FIXED |
| L-2 | Duplicate __init__.py | ✅ Already clean |
| L-3 | print() in backfill_markets.py | ✅ FIXED — uses logger |
| L-4 | Dashboard HTTP warning | ✅ Already implemented |
| L-5 | Metaculus URL configurable | ✅ Already implemented |
| L-6 | DB file permissions | ✅ FIXED — os.chmod(0o600) |
| L-7 | Kelly sizer edge floor too low | ✅ FIXED — raised to 0.7 |

---

## Strengthened Areas

### FedWatch & Cleveland Fed Scrapers (Weakest → Strong)

| Aspect | Before | After |
|--------|--------|-------|
| Test count | 6 + 6 = 12 | 22 + 24 = 46 |
| Retry logic | None | Shared retry_helper (2 retries, exponential backoff) |
| Stale cache fallback | None | Last-good-result served on fetch/parse failure |
| Validation | Minimal | Probability bounds [0, 100], CPI bounds [-5, 50] |
| Logging | WARNING only | DEBUG/INFO/WARNING multi-level |
| Error handling | Single try/except → None | Layered: fetch retry → parse → stale fallback |

### Database Module (M-10 Split)

| Aspect | Before | After |
|--------|--------|-------|
| database.py LOC | 1,811 | 701 (core + migrations) |
| Domain modules | 0 | 6 (markets, trades, calibration, risk, whales, stats) |
| Pattern | Monolithic class | Mixin composition |
| Backward compat | N/A | ✅ All imports unchanged |

### Order Router (M-11 Split)

| Aspect | Before | After |
|--------|--------|-------|
| order_router.py LOC | 1,060 | 473 (core routing) |
| Platform modules | 0 | 3 (router_kalshi, router_polymarket, router_paper) |
| Pattern | Single file | Platform-specific modules |
| Backward compat | N/A | ✅ All imports unchanged |

### Retry Logic (M-12 Consolidation)

| Aspect | Before | After |
|--------|--------|-------|
| Retry implementations | 3+ duplicate loops | 1 shared `retry_with_backoff()` |
| Used by | N/A | claude_forecaster, news_researcher, fedwatch, cleveland_fed |
| Features | Basic | Exponential backoff, jitter, on_retry callback, abort_check |

---

## New Issues Found in Re-Audit

### 🔴 CRITICAL — None

### 🟠 HIGH (2)

**H-1: Backtest lacks baseline comparison**
- **File:** `scripts/backtest_engine.py:297-310`
- **Impact:** Cannot validate whether trading edge beats market-price-as-predictor baseline. Investors can't assess if results are better than random.
- **Fix:** Add Brier score for "always predict market price" as baseline to backtest report

**H-2: `_assess_single_market()` is 601 lines**
- **File:** `src/strategies/ai_probability.py:268`
- **Impact:** Core trading decision function is extremely difficult to test, debug, or modify in isolation
- **Fix:** Extract sub-functions: context gathering, forecast generation, edge calculation, signal construction

### 🟡 MEDIUM (8)

**M-1: Token tracking underestimates on failed retries**
- **File:** `src/analysis/claude_forecaster.py:393-407`
- **Impact:** Daily token budget may be exceeded without warning if rate limits hit frequently
- **Fix:** Track attempted calls separately; estimate failed attempt tokens

**M-2: Backtest doesn't validate market price staleness**
- **File:** `scripts/backtest_engine.py:125-150`
- **Impact:** Forecasts may be evaluated against stale price anchors for settled markets
- **Fix:** Log age of last_price; warn if >1 hour old

**M-3: Confidence interval calibration not tracked**
- **File:** `scripts/backtest_engine.py:242-274`
- **Impact:** Cannot audit forecast uncertainty quality (CI coverage)
- **Fix:** Add CI coverage check to backtest report

**M-4: 5 `except Exception` clauses remain**
- **Files:** Various (5 locations in src/)
- **Impact:** Overly broad exception catching can mask specific errors
- **Fix:** Narrow to specific exception types where possible

**M-5: `scan_and_trade()` is 321 lines**
- **File:** `src/orchestrator/scan_cycle.py:248`
- **Impact:** Main trading loop is difficult to test in isolation
- **Fix:** Extract phases into separate functions

**M-6: `news_researcher.py` at 893 LOC could be split**
- **File:** `src/analysis/news_researcher.py`
- **Impact:** Mixed concerns: HTTP fetching, parsing, enrichment, caching
- **Fix:** Split into fetcher, parser, enricher modules

**M-7: `calculate_position_size()` is 243 lines**
- **File:** `src/risk/kelly_sizer.py:71`
- **Impact:** Complex mathematical function difficult to review
- **Fix:** Extract sub-calculations (base Kelly, adjustments, caps) into helpers

**M-8: WebSocket client lacks connection health metrics**
- **File:** `src/core/websocket_client.py`
- **Impact:** No visibility into reconnection frequency or message loss rate
- **Fix:** Add connection uptime and reconnect count tracking

### 🟢 LOW (5)

**L-1: Backtest report is text-only**
- **File:** `scripts/backtest_engine.py:307-310`
- **Fix:** Add CSV/JSON export option for calibration data

**L-2: `database.py` still 701 LOC after split**
- **File:** `src/storage/database.py`
- **Note:** Down from 1,811. Remaining code is table creation + migrations. Acceptable.

**L-3: Config validator error messages not standardized**
- **File:** `src/config.py`
- **Fix:** Use consistent format for validator error messages

**L-4: `router_kalshi.py::live_fill()` is 227 lines**
- **File:** `src/execution/router_kalshi.py:33`
- **Fix:** Extract order status polling into separate method

**L-5: 112 test warnings about unawaited coroutines**
- **Files:** Various test files
- **Fix:** Use `AsyncMock` consistently for coroutine mocks

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | ✅ Events API + price validation | ✅ 23 tests | ✅ Category filter | ✅ Production |
| Forecast Generation | ✅ Claude + cross-check + decomposition | ✅ 42+ tests | ✅ Token budget, timeout | ✅ Production |
| Edge Detection | ✅ Multi-strategy (AI, arb, news, whale) | ✅ 30+ tests | ✅ Min edge thresholds | ✅ Production |
| Position Sizing | ✅ Half-Kelly with caps + adjustments | ✅ 38 tests | ✅ Max 5% per position | ✅ Production |
| Order Execution | ✅ Kalshi + paper + Decimal math | ✅ 192 tests | ✅ Three-gate safety | ✅ Production |
| Position Tracking | ✅ DB + Kalshi sync + P&L | ✅ 25+ tests | ✅ Reconciliation | ✅ Production |
| P&L Calculation | ✅ Realized + unrealized | ✅ 15+ tests | ✅ Daily tracking | ✅ Production |
| Circuit Breaker | ✅ Daily loss + consecutive + drawdown | ✅ 23 tests | ✅ Auto-recovery + persist | ✅ Production |
| Calibration | ✅ Brier scores + category breakdown | ✅ 20+ tests | ✅ Trend alerts | ✅ Production |
| Tax Reporting | ✅ FIFO + settlement lots + CSV | ✅ 27 tests | N/A | ✅ New |

---

## Module-by-Module Scorecard

| Module | Quality | Tests | Error Handling | Risk Controls | Overall |
|--------|---------|-------|----------------|---------------|---------|
| config.py | 5/5 | 4/5 | 5/5 | 5/5 | ⭐⭐⭐⭐⭐ |
| core/kalshi_client.py | 4/5 | 4/5 | 5/5 | 4/5 | ⭐⭐⭐⭐ |
| core/market_discovery.py | 4/5 | 5/5 | 4/5 | 4/5 | ⭐⭐⭐⭐ |
| core/models.py | 5/5 | 4/5 | N/A | N/A | ⭐⭐⭐⭐⭐ |
| core/websocket_client.py | 4/5 | 3/5 | 4/5 | 3/5 | ⭐⭐⭐½ |
| analysis/claude_forecaster.py | 4/5 | 5/5 | 5/5 | 5/5 | ⭐⭐⭐⭐½ |
| analysis/ensemble.py | 5/5 | 4/5 | 4/5 | 4/5 | ⭐⭐⭐⭐ |
| analysis/calibration.py | 5/5 | 5/5 | 4/5 | 4/5 | ⭐⭐⭐⭐½ |
| data/fedwatch.py | 4/5 | 5/5 | 5/5 | 4/5 | ⭐⭐⭐⭐½ |
| data/cleveland_fed.py | 4/5 | 5/5 | 5/5 | 4/5 | ⭐⭐⭐⭐½ |
| data/news_ingestion.py | 4/5 | 4/5 | 4/5 | 3/5 | ⭐⭐⭐⭐ |
| execution/order_builder.py | 5/5 | 5/5 | 5/5 | 5/5 | ⭐⭐⭐⭐⭐ |
| execution/order_router.py | 4/5 | 5/5 | 5/5 | 5/5 | ⭐⭐⭐⭐½ |
| execution/position_manager.py | 4/5 | 4/5 | 4/5 | 5/5 | ⭐⭐⭐⭐ |
| risk/risk_engine.py | 5/5 | 5/5 | 5/5 | 5/5 | ⭐⭐⭐⭐⭐ |
| risk/circuit_breaker.py | 5/5 | 5/5 | 5/5 | 5/5 | ⭐⭐⭐⭐⭐ |
| risk/kelly_sizer.py | 4/5 | 5/5 | 4/5 | 5/5 | ⭐⭐⭐⭐½ |
| storage/database.py | 4/5 | 5/5 | 4/5 | 4/5 | ⭐⭐⭐⭐ |
| strategies/ai_probability.py | 3/5 | 4/5 | 4/5 | 4/5 | ⭐⭐⭐½ |
| orchestrator/lifecycle.py | 4/5 | 4/5 | 4/5 | 4/5 | ⭐⭐⭐⭐ |
| orchestrator/scan_cycle.py | 3/5 | 4/5 | 4/5 | 4/5 | ⭐⭐⭐½ |
| scripts/export_tax_report.py | 5/5 | 5/5 | 4/5 | N/A | ⭐⭐⭐⭐⭐ |

---

## Improvement Roadmap Status (Section 12)

| Feature | Status | Notes |
|---------|--------|-------|
| Market price in Claude prompts | ✅ Implemented | All prompt templates include current price |
| Superforecaster-style decomposition | ✅ Implemented | `src/analysis/decomposer.py` (422 LOC) |
| Multi-model ensemble | ✅ Implemented | `src/analysis/ensemble.py` with cross-check |
| Calibration tracking (Brier scores) | ✅ Implemented | `src/analysis/calibration.py` + trend alerts |
| Full article text fetching | ✅ Implemented | news_researcher.py fetches full text |
| Performance dashboard | ✅ Implemented | FastAPI at port 8080 |
| Tax reporting export | ✅ Implemented | FIFO + CSV export (new) |
| GPT-4o as second forecaster | ❌ Not implemented | Single-model (Claude) with cross-check |

---

## Top 10 Recommendations (Prioritized)

### Risk Reduction
1. **Add backtest baseline comparison** (H-1) — Cannot validate edge without benchmark
2. **Refactor `_assess_single_market()` from 601 lines** (H-2) — Core trading logic untestable

### Reliability
3. **Fix token tracking for failed retries** (M-1) — Could silently exceed API budget
4. **Add WebSocket health metrics** (M-8) — No visibility into connection reliability
5. **Narrow 5 `except Exception` clauses** (M-4) — Could mask specific errors

### Performance
6. **Add confidence interval calibration** (M-3) — Reveals systematic over/under-confidence
7. **Validate backtest price staleness** (M-2) — Ensures accurate edge measurement

### Code Quality
8. **Refactor `scan_and_trade()` from 321 lines** (M-5) — Main loop testability
9. **Split `news_researcher.py` at 893 LOC** (M-6) — Separate concerns
10. **Reduce test warnings from 112 to <10** (L-5) — Cleaner CI signal

---

## Conclusion

The codebase has improved significantly since the initial audit:

- **All 28 issues from initial audit resolved** (7H + 14M + 7L)
- **Weakest areas strengthened:** FedWatch/Cleveland Fed scrapers now have retry logic, stale cache fallback, validation, and 4x test coverage
- **Strongest areas maintained:** Risk engine (5/5), circuit breaker (5/5), order builder (5/5) unchanged
- **New capabilities added:** Tax reporting (27 tests), database mixins, platform-specific routers

**New findings are exclusively quality and maintainability issues** — no new Critical or money-loss risks. The 2 new High issues (backtest baseline, function size) are development quality concerns, not trading safety issues.

**Overall system health: PRODUCTION READY**
- 2,042 tests passing
- 1.19x test-to-code ratio
- Zero bare excepts, zero TODOs, zero print statements
- Comprehensive retry/fallback logic across all 8 external APIs
- Decimal arithmetic for all monetary calculations
- Three-gate live trading safety system
- 15-point pre-trade risk engine
