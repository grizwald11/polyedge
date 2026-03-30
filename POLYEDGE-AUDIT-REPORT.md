# PolyEdge Codebase Audit Report

**Audit Date:** March 30, 2026
**Auditor:** Claude Opus 4.6 (automated, line-by-line)
**Codebase:** /Users/adamgrodin/polyedge (commit 69e1395)
**Platform:** Python 3.12+ on Mac Mini M4 Pro
**Exchange:** Kalshi (primary), Polymarket (secondary, gated)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| Source files (src/) | 66 |
| Test files (tests/) | 66 |
| Total source lines | 18,212 |
| Tests passing | 931 (3 skipped) |
| External API integrations | 7 (Kalshi, Anthropic, Serper, FRED, Metaculus, Manifold, DuckDuckGo) |
| Environment variables | 11 total, 11 documented in .env.example |
| Trading mode | Paper (live gates disabled) |
| Kalshi API mode | Production (use_demo: false) |
| Bankroll (config) | $5,000 |
| Dependencies (pinned) | 17 direct, all exact versions |

---

## Issues by Severity

### CRITICAL (0 issues)

No critical issues found. The codebase has been through 22 audit revisions and all previously identified critical issues have been resolved.

---

### HIGH (7 issues)

**H-1: `main()` function is 408 lines**
- **File:** `src/main.py:1042`
- **What's wrong:** Single function handles initialization, loop orchestration, shutdown, reporting. Extremely difficult to test, debug, or modify safely.
- **Impact:** Maintenance burden; risk of introducing regression when modifying any part of the trading loop.
- **Fix:** Already tracked as TODO (L-1). Split into `src/orchestrator/` with startup.py, scan_cycle.py, trade_cycle.py, lifecycle.py.

**H-2: `assess_market()` is 346 lines**
- **File:** `src/analysis/claude_forecaster.py:186`
- **What's wrong:** Single method handles circuit breaker check, budget check, cache lookup, prompt construction, API call, retry logic, response parsing, token tracking, and validation.
- **Impact:** Any change to one concern risks breaking others; hard to unit test individual steps.
- **Fix:** Extract into pipeline stages: `_check_preconditions()`, `_build_and_call()`, `_parse_and_validate()`.

**H-3: Order router has only 3 tests**
- **File:** `tests/test_execution/test_order_router.py`
- **What's wrong:** The order router (`src/execution/order_router.py`, 905 lines) is the most critical execution component. 3 tests is grossly insufficient for a component that submits real orders.
- **Impact:** Untested code paths could submit malformed orders, miscalculate fees, or fail to reconcile after timeouts.
- **Fix:** Add tests for: paper fill simulation, live order submission (mocked), timeout reconciliation, balance pre-flight rejection, Polymarket residency gate, partial fill handling.

**H-4: Polymarket residency gate has no unit test**
- **File:** `src/execution/order_router.py:195,560`
- **What's wrong:** The `CONFIRM_NON_US_POLYMARKET` gate is a regulatory compliance control. No unit test verifies it actually blocks orders when unset.
- **Impact:** Regulatory risk -- if gate is accidentally bypassed, US-resident trading on Polymarket violates terms.
- **Fix:** Add tests: gate blocks when env var unset, gate passes when set to "true", gate blocks when set to "false".

**H-5: Dashboard routes have zero tests**
- **File:** `src/dashboard/routes_api.py`, `routes_html.py`, `routes_partials.py`
- **What's wrong:** All three route modules (API, HTML, HTMX partials) are completely untested.
- **Impact:** Regressions in dashboard could expose incorrect P&L data, misleading position information, or crash on missing data.
- **Fix:** Add FastAPI TestClient-based tests for critical API endpoints (portfolio, positions, signals).

**H-6: PM2 kill_timeout may cause force-kill during graceful shutdown**
- **File:** `ecosystem.config.js:62`
- **What's wrong:** `kill_timeout: 30000` (30s). Graceful shutdown in `main.py:1396-1445` closes fill tracker, WebSocket, HTTP clients, and DB. If cleanup exceeds 30s, PM2 sends SIGKILL -- potential data loss for in-flight orders.
- **Impact:** Could lose pending order state or corrupt partial fill tracking on restart.
- **Fix:** Increase to `kill_timeout: 60000` (60s).

**H-7: Manipulation detector price history grows unbounded**
- **File:** `src/risk/manipulation_detector.py:61`
- **What's wrong:** `history.append((now, yes_price))` per market, per scan cycle, with no size cap. Over days/weeks of continuous operation, memory grows without bound.
- **Impact:** Gradual memory leak; could hit PM2's 500MB limit and trigger forced restart.
- **Fix:** Add sliding window: `if len(history) > 1000: history = history[-500:]`.

---

### MEDIUM (13 issues)

**M-1: 7 functions exceed 200 lines**
- **Files:** `main.py:1042` (408), `claude_forecaster.py:186` (346), `order_router.py:289` (256), `ai_probability.py:196` (251), `database.py:341` (250), `main.py:631` (245), `kelly_sizer.py:42` (202)
- **Impact:** Difficult to test, review, and maintain. High coupling within each function.
- **Fix:** Extract sub-functions at logical boundaries (precondition checks, core logic, post-processing).

**M-2: Foreign key constraints disabled in database**
- **File:** `src/storage/database.py:281-297`
- **What's wrong:** `PRAGMA foreign_keys = OFF` -- documented tech debt (M-18). Orphaned records possible.
- **Impact:** Data integrity not enforced at DB layer; stale calibration records or signals could reference deleted markets.
- **Fix:** Complete schema v7 migration to re-enable FK enforcement.

**M-3: Database write lock has no timeout**
- **File:** `src/storage/database.py:256`
- **What's wrong:** `threading.Lock()` without timeout. If a writer crashes while holding the lock, all subsequent writes deadlock.
- **Impact:** Trading halt until restart.
- **Fix:** Use `lock.acquire(timeout=10)` with fallback error handling.

**M-4: 35 files have incorrect import ordering**
- **Files:** Widespread across src/ (analysis/, core/, data/, strategies/, execution/)
- **What's wrong:** Third-party and local imports mixed, not separated by blank lines per PEP 8.
- **Impact:** Readability and maintainability.
- **Fix:** Run `isort --profile black` across codebase.

**M-5: Magic numbers scattered throughout code**
- **Files:** `src/metrics.py:191` (600s), `src/main.py:678` (0.05), `src/core/websocket_client.py:173` (50), `src/core/kalshi_client.py:301` (5)
- **Impact:** Hard to understand thresholds without context; risk of inconsistent changes.
- **Fix:** Extract to named constants in config.py or module-level constants.

**M-6: 13 public functions missing return type hints**
- **Files:** Dashboard routes (3), main.py (3), ensemble.py (1), market_discovery.py (1), polymarket_discovery.py (1), prompt_templates.py (1), config.py (1)
- **Impact:** MyPy can't validate return types; IDE autocompletion degraded.
- **Fix:** Add return type annotations to all public functions.

**M-7: Polymarket client has only 1 test (instantiation)**
- **File:** `tests/test_core/test_polymarket_client.py`
- **Impact:** If Polymarket is enabled, order placement and position queries are untested.
- **Fix:** Add mocked tests for order lifecycle.

**M-8: No Manifold client tests**
- **File:** `src/data/manifold_client.py` -- no corresponding test file
- **Impact:** Community forecast integration could fail silently if API changes.
- **Fix:** Add basic tests for search and probability extraction.

**M-9: RSS feed coverage limited**
- **File:** `src/data/news_ingestion.py:54-58`
- **What's wrong:** Only 3 RSS feeds configured (Reuters top/business, NYT Politics). Missing coverage for economics, geopolitics, tech/AI.
- **Impact:** News-reactive strategy misses relevant articles from uncovered domains.
- **Fix:** Add NYT Business, World, Science feeds; consider AP News feed.

**M-10: Kalshi circuit breaker uses flat 60s halt**
- **File:** `src/core/kalshi_client.py:301-302`
- **What's wrong:** After 5 consecutive 5xx errors, blocks requests for exactly 60s. No exponential backoff.
- **Impact:** If Kalshi has extended downtime, bot hammers API at 60s intervals indefinitely.
- **Fix:** Implement exponential backoff: 60s, 120s, 240s, 480s, capped at 600s.

**M-11: PM2 max_restarts=5 may be insufficient**
- **File:** `ecosystem.config.js:58`
- **Impact:** Temporary API outages could exhaust restart budget, leaving bot down until manual intervention.
- **Fix:** Increase to `max_restarts: 15` or add `restart_delay` exponential backoff.

**M-12: No article minimum word count filter**
- **File:** `src/analysis/news_researcher.py:570-642`
- **What's wrong:** Fetched articles with <10 words (paywalled, 404, login walls) are still included as context.
- **Impact:** Claude receives empty/garbage context, potentially degrading forecast quality.
- **Fix:** Reject articles with fewer than 50 extracted words.

**M-13: `_processed_fills` set grows unbounded**
- **File:** `src/execution/fill_tracker.py:44`
- **What's wrong:** Set of processed order IDs grows forever with no TTL or size limit.
- **Impact:** Minor memory leak over months of continuous operation.
- **Fix:** Prune entries older than 7 days, or use a bounded set (max 10,000 entries).

---

### LOW (8 issues)

**L-1: Silent exception swallowing in best-effort paths**
- **Files:** `src/main.py:851` (alerts), `src/main.py:689` (disk check)
- **What's wrong:** `except Exception: pass` with no logging.
- **Impact:** Makes debugging harder when best-effort operations fail.
- **Fix:** Add `logger.debug()` calls in except blocks.

**L-2: WebSocket key loading duplicated**
- **Files:** `src/core/kalshi_client.py:55-128` and `src/core/websocket_client.py:524-542`
- **What's wrong:** Both files independently load and validate the RSA private key.
- **Impact:** Code duplication; changes to key loading must be applied in two places.
- **Fix:** Extract to shared `src/core/key_loader.py` module.

**L-3: 8 files use .format() instead of f-strings**
- **Files:** prompt_templates.py, ensemble.py, fred_client.py, whale_tracker.py, news_reactive.py, cross_arb.py, backtest.py, main.py
- **Impact:** Minor inconsistency; f-strings are preferred throughout the rest of the codebase.
- **Fix:** Convert remaining `.format()` calls to f-strings (except template strings that require `.format()`).

**L-4: Only 2 TODO comments remain**
- **Files:** `src/main.py:7` (L-1: split main), `src/storage/database.py:287` (M-18: FK constraints)
- **Impact:** Both are tracked and documented. No concern.
- **Fix:** Continue with planned refactoring.

**L-5: Script entry points missing docstrings**
- **Files:** `src/scripts/backtest.py:314`, `src/scripts/calibration_report.py:117`
- **Impact:** Minimal -- these are CLI entry points.
- **Fix:** Add brief module/function docstrings.

**L-6: Inconsistent HTTP timeouts across Polymarket discovery**
- **File:** `src/core/polymarket_discovery.py:237` (30s) vs line 275 (10s)
- **Impact:** No documented timeout strategy; could be confusing.
- **Fix:** Unify to a single configurable timeout.

**L-7: No source trust ranking in news search**
- **File:** `src/analysis/news_researcher.py`
- **What's wrong:** Reuters/AP/NYT results aren't boosted over less reliable sources.
- **Impact:** Lower-quality sources may appear in Claude's context.
- **Fix:** Add source quality multiplier to relevance scoring.

**L-8: Backtest slippage model is simplistic**
- **File:** `scripts/backtest_engine.py:321`
- **What's wrong:** Flat basis-point slippage, no market-impact curve.
- **Impact:** Backtest results may overstate performance for large orders.
- **Fix:** Document limitation (already done at line 19-20). Consider order-book-depth-based slippage model in future.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS SHA-256 | All endpoints wrapped; specific exceptions | 3 retries + exponential backoff (cap 10s) | Retry-After parsing (numeric + HTTP-date); semaphore(5); 100ms min interval | 30s httpx timeout | 177+ tests | Production-ready |
| Kalshi WebSocket | RSA-PSS SHA-256 | Auto-reconnect; auth failure detection; 10-failure permanent halt | Exponential backoff (1s-60s); subscription retry 3x | Ping interval 20s; timeout 30s | Configurable ping/pong | 30+ tests | Production-ready |
| Anthropic (Claude) | API key (env var) | Circuit breaker (3 failures = 5min halt); 4-strategy response parsing | Rate limit: 3 retries; Connection: 3 retries; Auth: no retry | Budget tracking (500K soft / 1M hard daily) | 60s configurable | 35+ tests | Production-ready |
| Serper (Search) | API key (env var) | 3-failure permanent disable; key rotation detection | 2 retries on 5xx; exponential backoff on 429 (cap 30s) | 1-hour cooldown after auth failure | Via httpx | 20+ tests | Production-ready |
| DuckDuckGo | None (free) | Falls back to text search if news search fails | Executor timeout 8s | N/A (free tier) | 8s via asyncio.wait_for | 15+ tests | Production-ready |
| FRED | API key (env var) | Graceful degradation (optional) | Via httpx defaults | N/A | 5s via data_enricher | 10+ tests | Optional, stable |
| Metaculus | Bearer token (env var) | Graceful degradation (optional) | Via httpx defaults | N/A | 4s via data_enricher | 5+ tests | Optional, stable |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi /events API with pagination; volume/liquidity/category filters; opportunity scoring | 30+ tests | Category exclusions (Crypto, Sports); min volume $100; min liquidity $100 | Production-ready |
| Forecast Generation | Claude Sonnet-4.6 (routine) / Opus-4.6 (>$50); superforecaster decomposition; 7 category templates; 3-layer prompt injection defense | 35+ tests | Circuit breaker; daily token budget; category-specific temperatures | Production-ready |
| Edge Detection | `claude_prob - market_price`; per-strategy min edge (5% AI, 2% arb, 1% obvious-no, 3% news); calibration bias correction | 25+ tests | Max divergence gate (40%); parse_failed rejection; category Brier gating (>0.30 = halt) | Production-ready |
| Position Sizing | Half-Kelly with calibration multiplier; fee-aware binary search; liquidity adjustment | 50 tests | 5% per position; 40% total exposure; price tier floors ($0.03 reject); Brier-based sizing multiplier | Production-ready |
| Order Execution | Paper (simulated fills) + Live (Kalshi API); maker preferred; timeout reconciliation | 3 tests (WEAK) | Three-gate safety (config + env + session); balance pre-flight; Polymarket residency gate | Needs more tests |
| Position Tracking | Weighted avg entry; proportional fee allocation; settlement with fee ledger closure; Kalshi sync | 36 tests | Size clamping on oversells; 4-decimal rounding; synthetic settlement trades | Production-ready |
| P&L Calculation | Realized = (exit - entry) * size - buy_fees - sell_fees; Unrealized = (current - entry) * size; direction-aware | 36 tests | Proportional buy fee allocation; rounding drift prevention | Production-ready |
| Settlement Handling | WebSocket lifecycle events; settlement value validation [0,1]; binary-only enforcement | 10+ tests | Rejects non-binary settlements (H-8, H-19); synthetic SELL trade with fee closure (H-4) | Production-ready |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|---|---|---|---|---|---|---|
| **src/core/kalshi_client.py** | 5 | 5 | 5 | 5 | 5 | 5 |
| **src/core/websocket_client.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/core/models.py** | 5 | 5 | 4 | N/A | 5 | 5 |
| **src/core/market_discovery.py** | 4 | 4 | 4 | 3 | 4 | 4 |
| **src/core/polymarket_client.py** | 4 | 1 | 4 | 3 | 4 | 3 |
| **src/core/polymarket_discovery.py** | 4 | 3 | 4 | 3 | 4 | 3 |
| **src/analysis/claude_forecaster.py** | 3 | 4 | 5 | 5 | 4 | 4 |
| **src/analysis/prompt_templates.py** | 5 | 4 | 5 | 5 | 5 | 5 |
| **src/analysis/ensemble.py** | 5 | 5 | 4 | 4 | 5 | 5 |
| **src/analysis/calibration.py** | 4 | 5 | 4 | N/A | 4 | 4 |
| **src/analysis/calibration_analyzer.py** | 4 | 5 | 4 | N/A | 4 | 4 |
| **src/analysis/news_researcher.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/analysis/market_classifier.py** | 5 | 4 | 4 | N/A | 4 | 4 |
| **src/analysis/resolution_tracker.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/data/market_scanner.py** | 4 | 4 | 4 | 4 | 4 | 4 |
| **src/data/news_ingestion.py** | 4 | 3 | 4 | 3 | 4 | 4 |
| **src/data/data_enricher.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/data/cache.py** | 5 | 4 | 4 | N/A | 4 | 4 |
| **src/data/fedwatch.py** | 4 | 4 | 4 | N/A | 4 | 4 |
| **src/data/fred_client.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/data/whale_monitor.py** | 4 | 3 | 4 | 3 | 4 | 4 |
| **src/data/market_graph.py** | 3 | 3 | 3 | N/A | 3 | 3 |
| **src/data/manifold_client.py** | 4 | 1 | 4 | N/A | 4 | 3 |
| **src/data/metaculus_client.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/data/polymarket_cross_ref.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/strategies/ai_probability.py** | 3 | 4 | 4 | 5 | 4 | 4 |
| **src/strategies/cross_arb.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/cross_platform_arb.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/obvious_no.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/whale_tracker.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/news_reactive.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/execution/order_builder.py** | 5 | 4 | 4 | 4 | 5 | 4 |
| **src/execution/order_router.py** | 4 | 1 | 4 | 5 | 4 | 3 |
| **src/execution/position_manager.py** | 4 | 5 | 4 | 5 | 4 | 4 |
| **src/execution/fill_tracker.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/risk/risk_engine.py** | 4 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/kelly_sizer.py** | 4 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/circuit_breaker.py** | 5 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/portfolio_risk.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/risk/manipulation_detector.py** | 4 | 4 | 4 | 4 | 4 | 4 |
| **src/storage/database.py** | 3 | 4 | 4 | 3 | 4 | 3 |
| **src/dashboard/server.py** | 4 | 2 | 4 | 4 | 3 | 3 |
| **src/alerts/alert_manager.py** | 4 | 2 | 4 | N/A | 4 | 3 |
| **src/alerts/daily_report.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/main.py** | 2 | 2 | 4 | 5 | 3 | 3 |
| **src/config.py** | 5 | 4 | 4 | N/A | 5 | 5 |
| **src/metrics.py** | 4 | 4 | 4 | N/A | 4 | 4 |

Scale: 1 (poor) - 5 (excellent)

---

## Section 1: Structural Integrity

### Directory Tree

```
polyedge/
├── CLAUDE.md                          # Project context
├── POLYEDGE-AUDIT-PROMPT.md           # This audit's prompt
├── pyproject.toml                     # Build config, pytest/mypy settings
├── requirements.txt                   # 17 pinned dependencies
├── ecosystem.config.js                # PM2 process manager config
├── Makefile                           # Build automation
├── .gitignore                         # Secrets, DB, venv excluded
├── config/
│   ├── settings.yaml                  # Main configuration
│   ├── categories.yaml                # Market category mappings
│   ├── .env                           # Secrets (NOT in git)
│   ├── .env.example                   # Template (in git)
│   └── kalshi_private_key.pem         # RSA key (NOT in git)
├── src/                               # 66 Python files, 18,212 lines
│   ├── main.py                        # Orchestrator (1,449 lines)
│   ├── config.py                      # Pydantic settings loader
│   ├── metrics.py                     # Performance metrics
│   ├── core/           (7 files)      # API clients, data models
│   ├── analysis/       (9 files)      # Claude forecasting, calibration
│   ├── data/           (14 files)     # Market scanning, news, whales
│   ├── strategies/     (7 files)      # 5 trading strategies
│   ├── execution/      (5 files)      # Order routing, position tracking
│   ├── risk/           (6 files)      # Risk engine, circuit breaker
│   ├── storage/        (2 files)      # SQLite database
│   ├── dashboard/      (5 files)      # FastAPI web UI
│   ├── alerts/         (4 files)      # Alert dispatch, iMessage
│   └── scripts/        (3 files)      # Backtest, calibration
├── tests/                             # 66 Python files, 931 tests
│   ├── conftest.py                    # 10 shared fixtures
│   ├── test_core/      (10 files)
│   ├── test_analysis/  (9 files)
│   ├── test_data/      (12 files)
│   ├── test_execution/ (6 files)
│   ├── test_risk/      (6 files)
│   ├── test_strategies/(7 files)
│   ├── test_scripts/   (4 files)
│   ├── test_dashboard/ (2 files)
│   ├── test_integration/(2 files)
│   └── test_alerts/    (4 files)
├── scripts/                           # Utility scripts
│   ├── backtest_engine.py
│   ├── backfill_markets.py
│   └── discover_whales.py
└── data/                              # Runtime (NOT in git)
    ├── markets.db
    ├── chroma/
    └── logs/
```

### Orphaned / Dead Code
No orphaned files detected. All modules are imported by at least one other module or test file.

### Config Files
- **pyproject.toml:** Present. Requires Python >=3.12. No dependencies listed (all in requirements.txt). Pytest and MyPy configured.
- **requirements.txt:** Present. 17 dependencies, ALL pinned to exact versions (e.g., `anthropic==0.86.0`). 5 optional dependencies commented out for future phases.
- **ecosystem.config.js:** Present. Runs `venv/bin/python -m src.main`. Autorestart enabled, max 5 restarts, 500MB memory limit.
- **.gitignore:** Present. Covers `.env`, `*.pem`, `*.key`, `data/*.db`, `venv/`, `__pycache__/`.
- **.env.example:** Present in `config/`. Documents all required and optional environment variables.

### Dependency Audit

| Package | Version | Purpose | Status |
|---------|---------|---------|--------|
| kalshi-python | 2.1.4 | Kalshi SDK | Current |
| py-clob-client | 0.34.6 | Polymarket CLOB | Current |
| cryptography | 46.0.5 | RSA-PSS signing | Current |
| anthropic | 0.86.0 | Claude API | Current |
| httpx | 0.28.1 | Async HTTP | Current |
| websockets | 16.0 | WebSocket | Current |
| pyyaml | 6.0.3 | YAML config | Current |
| pydantic | 2.12.5 | Data validation | Current |
| python-dotenv | 1.2.2 | Env loading | Current |
| fastapi | 0.135.1 | Dashboard | Current |
| uvicorn | 0.42.0 | ASGI server | Current |
| jinja2 | 3.1.6 | Templates | Current |
| feedparser | 6.0.12 | RSS parsing | Current |
| ddgs | 9.11.4 | DuckDuckGo search | Current |
| pytest | 9.0.2 | Testing | Current |
| pytest-asyncio | 1.3.0 | Async tests | Current |

No known CVEs detected for pinned versions. No unused dependencies found.

---

## Section 2: Configuration & Environment

### Complete Environment Variable Inventory

| Variable | Required | Used By | Documented |
|----------|----------|---------|------------|
| `KALSHI_API_KEY_ID` | Yes | config.py:272, kalshi_client.py | Yes |
| `KALSHI_PRIVATE_KEY_PATH` | Yes | config.py:273, kalshi_client.py:65-88 | Yes |
| `ANTHROPIC_API_KEY` | Yes | config.py:274, claude_forecaster.py:67 | Yes |
| `POLYMARKET_PRIVATE_KEY` | No | config.py:279, polymarket_client.py:64-69 | Yes |
| `SERPER_API_KEY` | No | config.py:275, news_researcher.py | Yes |
| `FRED_API_KEY` | No | config.py:277, fred_client.py:49-71 | Yes |
| `METACULUS_API_TOKEN` | No | config.py:278, metaculus_client.py:33-52 | Yes |
| `SEARXNG_URL` | No | config.py:276 | Yes |
| `POLYEDGE_LIVE_ENABLED` | No | config.py:280, order_router.py:107-114 | Yes |
| `CONFIRM_NON_US_POLYMARKET` | No | order_router.py:195,560 | Yes |
| `POLYEDGE_DASHBOARD_KEY` | No | dashboard/server.py:85-90 | Yes |

All 11 variables documented in `.env.example`. Zero undocumented variables.

### Hardcoded Values
No API keys or secrets hardcoded. All endpoints configurable via `config/settings.yaml`. Kalshi supports demo/production toggle (`use_demo: true/false`).

### Secrets in Git
Confirmed: `git show HEAD:config/.env` returns "Not in HEAD". No secrets found in git history. `.gitignore` properly covers all sensitive files.

---

## Section 3: Kalshi Integration

### Endpoints Used (12 total)
**Public (6):** `/exchange/status`, `/markets`, `/markets/{ticker}`, `/events`, `/markets/{ticker}/orderbook`, `/markets/trades`
**Authenticated (6):** `/portfolio/balance`, `/portfolio/positions`, `/portfolio/orders` (GET/POST), `/portfolio/orders/{id}` (GET/DELETE)
**WebSocket:** `wss://demo-api.kalshi.co/trade-api/ws/v2` (channels: ticker, fill, market_lifecycle_v2)

### Authentication
- RSA-PSS with SHA-256 signing, base64 encoded
- Key file permissions enforced (0o600), auto-fixed with warning
- Key freshness checking (mtime-based rotation support)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`

### Rate Limiting
- `Retry-After` header parsing (numeric seconds + HTTP-date format)
- Exponential backoff: `min(10, 2^(attempt+1) + jitter)`
- Semaphore: max 5 concurrent requests
- Minimum interval: 100ms between requests
- Circuit breaker: 5 consecutive 5xx errors = 60s halt

### Error Handling
Every endpoint has specific exception handling: `HTTPStatusError`, `RequestError`, `KalshiRateLimitError`, and generic `Exception`. All errors logged with `exc_info=True`.

### Order Placement
- Price conversion: `dollars_to_cents()` uses `Decimal` arithmetic (no floating-point)
- NO side conversion: `yes_price = 1.0 - order.price` correctly applied
- Range validation: `1 <= yes_price <= 99` cents enforced
- Side set explicitly by `order_builder` (not inferred from token_id)

### Monetary Calculations
All fee calculations use `Decimal` with `ROUND_CEILING`. Taker: `ceil(0.07 * size * p * (1-p))`. Maker: `ceil(0.0175 * size * p * (1-p))`. P&L rounded to 4 decimal places throughout.

### WebSocket
Auto-reconnect with exponential backoff (1s-60s). Auth failure detection (401/403 = permanent halt after 10 failures). Re-subscribes on reconnect with 3x retry. Settlement value validation: rejects non-binary values and out-of-range [0,1].

---

## Section 4: AI Forecasting Pipeline

### Claude Integration
- SDK: `anthropic==0.86.0` via `AsyncAnthropic`
- Models: `claude-sonnet-4-6` (routine, $3/$15 per Mtok) and `claude-opus-4-6` (>$50 positions, $15/$60 per Mtok)
- Temperature: Category-specific (Fed/Macro=0.20, Politics=0.25, Geopolitics/Tech=0.30, Culture=0.40)
- Token budget: 500K soft / 1M hard daily limit. Pre-call budget check refuses calls near limit.
- Circuit breaker: 3 consecutive failures = 5-minute API halt

### Prompt Engineering
- System prompt includes calibration rules, base rate emphasis, overconfidence prevention, superforecaster compound-event decomposition (AND/OR/conditional)
- 7 category-specific templates + NEWS_IMPACT + ARB_VALIDATION
- Market price included in all templates as `{market_price:.0%}`
- Resolution criteria included verbatim (sanitized)
- 3-layer prompt injection defense: control char stripping, sentence-level pattern detection/removal, character allowlist

### Response Parsing
4-strategy fallback chain: direct JSON, markdown code block, brace extraction, regex prose extraction. Final fallback returns `probability=0.5` with `parse_failed=True`. Probability clamped to [0.01, 0.99]. Confidence intervals auto-corrected if inverted.

### Ensemble
- Single-model mode: 85% Claude / 15% market (adjusted by CI width, extreme prices, divergence)
- Multi-model mode: Claude + Manifold/Metaculus community forecasts with Brier-score-weighted averaging
- No GPT-4o/OpenAI integration (community forecasts fill the ensemble role)

### Calibration
- Per-category Brier score tracking with time-decay weighting (half-life 30 days)
- Calibration curve: 10-bin histogram (predicted vs actual resolution rate)
- Category bias corrections applied to future forecasts
- Category accuracy gating: Brier > 0.30 = skip category entirely; Brier > 0.20 = require 8% edge

---

## Section 5: Data Pipeline & News Integration

### Search Architecture
- **Primary:** DuckDuckGo (free, no API key) via `ddgs` library
- **Fallback:** Serper.dev (paid, optional) with 3-failure permanent disable and auto-recovery on key rotation
- **RSS Feeds:** Reuters (top + business), NYT Politics -- polled every 120s

### Full Article Text
Implemented. Custom HTML parser extracts visible text, skipping script/style/nav elements. Prioritizes lede (first 2 sentences) + body middle. Truncated at sentence boundaries (max 3,000 chars).

### Data Freshness
Category-aware staleness thresholds: Fed/Macro (5 days), Geopolitics (7 days), Tech/AI (10 days), Politics (14 days), Culture (30 days). Relative and absolute date parsing with 7+ formats supported.

### Query Construction
Multi-angle approach: base question, time-scoped (`{query} latest news 2026`), entity-focused (proper nouns), broad expansion (acronym expansion: "Fed" -> "Federal Reserve"). Max 4 queries, 5 results each.

### Caching
Three-tier TTL cache: news (2 min), economic data (60 min), community forecasts (30 min). Check-on-get pattern; only non-empty results cached.

### URL Deduplication
Tracking parameter stripping (utm_*, fbclid, gclid, etc.). URL normalization (www. removal, trailing slash strip, fragment removal). Title-based deduplication (Jaccard similarity > 0.7).

---

## Section 6: Trading Logic & Risk Management

### Core Decision Engine
11-point pre-trade risk gate -- ALL must pass:
1. Balance (includes pending orders)
2. Position size (max 5% bankroll)
3. Total exposure (max 40%)
4. Correlated exposure (max 20%, by event_ticker)
5. Circuit breaker (daily loss, drawdown, consecutive losses)
6. Liquidity (reject if >10% of book depth)
7. Existing position (no double-entry)
8. Signal quality (confidence >= 55%, edge finite and positive, impossible edge detection)
9. Resolution date (min 1 day, warn >365 days)
10. Cooldown (4h after loss exit, 1h after profit exit)
11. Manipulation detection (rapid moves, slow drift, crossed books)

Plus: obvious-NO cap (10% bankroll), max concurrent positions (6).

### Position Sizing
Half-Kelly with hard caps. Fee-aware binary search for contract count. Liquidity adjustment (>10% of book = halve size). Price tier floors (<$0.03 rejected). Calibration-based multiplier (Brier score drives 0x-1.1x sizing). Circuit breaker multiplier (0.5x on 3+ losing days).

### Edge Thresholds by Strategy

| Strategy | Min Edge | Notes |
|----------|----------|-------|
| AI Probability | 5% | Core strategy |
| Cross-Arb | 2% | Tighter spread exploitation |
| Obvious NO | 1% | High-confidence near-certain |
| News Reactive | 3% | Faster-decaying edge |

### Stop-Loss & Circuit Breakers
- Hard stop-loss: 28% (30% minus 2% slippage buffer), requires fresh price data (<2 min)
- Daily loss limit: 10% of bankroll (realized + 50% unrealized), halts for 24 wall-clock hours
- Max drawdown: 20% peak-to-trough, permanent halt until manual reset
- Consecutive losses: 3-4 days = quarter-Kelly; 5+ days = full halt
- All circuit breaker state persisted to DB for crash recovery

### Partial Fills
Cumulative delta tracking -- only records new contracts. Handles API corrections (lower filled_count). Transactional safety (BEGIN/COMMIT wrapping). Deduplication on restart via DB-loaded state.

### Market Manipulation Detection
Percentage-based rapid move detection (20% in single interval). Slow drift detection (4+ consecutive same-direction moves, 15% cumulative). Crossed order book detection (>5% YES+NO deviation from 1.0). Flags expire after 30 minutes.

---

## Section 7: Backtesting & Performance Tracking

### Backtest Infrastructure
- Engine: `scripts/backtest_engine.py` (898 lines) with `BacktestPortfolio` class
- Historical data: `market_snapshots` table with hourly granularity
- Fee accounting: Kalshi taker/maker fees calculated per trade. Entry and exit fees tracked separately.
- Slippage: Configurable basis-point model (documented as heuristic, not data-driven)
- Bias controls: Lookahead flag with halved degradation factor; all markets loaded (not just settled); unresolved positions marked-to-last-price
- Degradation factors: AI (0.65x), obvious_no (0.85x), cross_arb (0.60x), whale (0.55x), news (0.50x)

### Calibration Tracking
- Brier score: Implemented with time-decay (30-day half-life). Perfect=0.0, random=0.25.
- Calibration curves: 10-bin histogram of predicted vs actual resolution rates
- Per-category stats: Brier, count, average predicted/actual, bias
- Category adjustments: Applied when |bias| exceeds statistical significance threshold (95% CI)
- Base rate injection: Historical YES resolution rates by category fed into prompts

### Decision Logging
- All signals logged to DB with strategy, edge, confidence, timestamp
- Orders linked to signals via `signal_id` foreign key
- Trades record: price, size, fee, realized P&L, strategy, paper flag
- Metrics module tracks: predicted_edge vs realized_return for edge correlation (M-21)

---

## Section 8: Error Handling & Reliability

### Exception Handling
- 702+ `logger.*()` calls across 53 files
- Zero bare `except:` clauses -- all catch specific exceptions or `Exception`
- Most `except Exception as e` blocks include `exc_info=True` for stack traces
- Best-effort operations (alerts, disk checks) intentionally swallow exceptions (documented)
- Zero mutable default arguments found

### Retry Logic

| Service | Retries | Backoff | Notes |
|---------|---------|---------|-------|
| Kalshi REST | 3 | Exponential (cap 10s) | Retry-After parsing |
| Kalshi WebSocket | 10 failures | Exponential (1-60s) | Auth failure = permanent |
| Claude API | 3 (rate limit), 3 (connection) | Exponential (2-10s) | Timeout = no retry |
| Serper | 2 on 5xx | Exponential; 30s cap on 429 | 3 auth failures = permanent disable |
| DuckDuckGo | 1 (news->text fallback) | 8s executor timeout | N/A |

### Graceful Degradation
- Claude API down: returns market price as probability
- Serper down: falls back to DuckDuckGo
- All optional data sources: continue without (FRED, Metaculus, Manifold)
- Polymarket unavailable: Kalshi-only operation
- Dashboard fails to start: trading continues

### State Persistence
- Circuit breaker state persisted to DB (daily loss, consecutive losses, halt status)
- Fill tracker loads partial fill counts from DB on restart
- Bankroll override persisted to DB for crash recovery
- Position sync with Kalshi API every cycle in live mode

### Memory Management
- Database pruning: old signals (>24h), snapshots (>7 days) automatically cleaned
- HTTP clients properly closed in graceful shutdown
- WebSocket closed in finally blocks
- Known leak: manipulation detector price history (H-7)
- Known leak: `_processed_fills` set (M-13)

---

## Section 9: Security Review

### Credential Storage
- All secrets in environment variables, loaded from `config/.env`
- `.env` excluded from git via `.gitignore` (confirmed not in git history)
- RSA private key permissions enforced (0o600) with auto-fix
- Database file permissions enforced (0o600)
- Serper API key redacted in error logs (`***REDACTED***` replacement)

### HTTPS
- Kalshi: `ssl.create_default_context()` enforces HTTPS verification
- All API URLs use `https://`
- Dashboard logs warning when running on HTTP without localhost restriction

### Dashboard Security
- Optional API key authentication via `POLYEDGE_DASHBOARD_KEY`
- Without key: localhost-only access (127.0.0.1, ::1)
- CORS restricted to localhost:8080 by default (configurable)

### Other
- Zero subprocess/os.system calls -- no command injection risk
- No user input evaluation or shell expansion
- No customer/account data logged in plaintext
- No timing information exposed through rate limiting

---

## Section 10: Code Quality

### Size Metrics
- 19 files over 300 lines (largest: database.py at 1,633)
- 7 functions over 200 lines (largest: main() at 408)
- 62+ functions over 50 lines

### Standards
- 0 print() statements -- all logging uses proper `logger` module
- 2 TODO comments (both tracked: L-1 main split, M-18 FK constraints)
- 99.7% docstring coverage (2 script entry points missing)
- 99%+ type hint coverage (13 functions missing return types)
- 95%+ f-string usage consistency
- Zero mutable default arguments
- Zero bare except clauses

---

## Section 11: Regulatory Compliance

### Kalshi (CFTC-Regulated)
- Primary exchange. All order placement routed through authenticated Kalshi API.
- Trade records stored in `trades` table with timestamps, prices, sizes, fees -- suitable for tax reporting.
- Position limits: Enforced via risk engine (5% per position, 40% total, 6 max concurrent).

### Polymarket (Non-US Only)
- Secondary exchange, gated behind `CONFIRM_NON_US_POLYMARKET` environment variable.
- Gate enforced in both paper and live modes.
- Polymarket trading disabled by default (`settings.polymarket.enabled: false`).
- Three-gate safety system prevents accidental live Polymarket trading.

### Market Manipulation Prevention
- Manipulation detector flags suspicious patterns (rapid moves, slow drift, crossed books).
- Risk engine rejects flagged markets.
- Max position sizes prevent market impact on thin books.
- No wash trading or spoofing mechanisms exist in the code.

---

## Section 12: Improvement Roadmap Audit

| Feature | Status | Evidence |
|---------|--------|---------|
| Feeding market price into Claude's prompt | DONE | All templates include `{market_price:.0%}` |
| GPT-4o as second forecaster | NOT IMPLEMENTED | Community forecasts (Manifold/Metaculus) fill this role instead |
| Superforecaster-style prompt decomposition | DONE | System prompt lines 34-40 with AND/OR/conditional breakdown |
| Fetching full article text from search results | DONE | Custom HTML parser in news_researcher.py:570-642 |
| Multi-model ensemble with disagreement handling | DONE | Brier-weighted averaging in ensemble.py; disagreement penalty applied to confidence |
| Calibration tracking with Brier scores | DONE | Per-category Brier, time-decay, calibration curves, bias corrections |
| Performance dashboard | DONE | FastAPI at localhost:8080 with portfolio, positions, signals, calibration views |

---

## Top 10 Recommendations (Prioritized)

### 1. Add order router tests (Risk Reduction)
**Why:** 905-line component with only 3 tests submits real orders to Kalshi. Untested paths include timeout reconciliation, balance pre-flight, Polymarket residency gate, and partial fill routing.
**Action:** Write 20+ tests covering paper/live routing, all rejection paths, and reconciliation logic.

### 2. Refactor main.py (Reliability)
**Why:** 1,449 lines with a 408-line `main()` function. Any change risks breaking the trading loop. Already tracked as TODO (L-1).
**Action:** Split into `src/orchestrator/` with startup, scan_cycle, trade_cycle, and lifecycle modules.

### 3. Add Polymarket residency gate test (Regulatory)
**Why:** Compliance control with no unit test. If accidentally bypassed, US-resident trading on Polymarket violates terms.
**Action:** Add 3 tests: gate blocks when unset, passes when "true", blocks when "false".

### 4. Fix manipulation detector memory leak (Reliability)
**Why:** Unbounded `history.append()` per market. Over weeks of operation, could hit PM2's 500MB limit.
**Action:** Add `if len(history) > 1000: history = history[-500:]` sliding window.

### 5. Increase PM2 kill_timeout to 60s (Reliability)
**Why:** Graceful shutdown closes fill tracker, WebSocket, HTTP clients, and DB. If >30s, PM2 force-kills, potentially losing pending order state.
**Action:** Change `kill_timeout: 60000` in `ecosystem.config.js`.

### 6. Add dashboard route tests (Reliability)
**Why:** Three route modules completely untested. Dashboard shows P&L and position data -- regressions could display incorrect financial information.
**Action:** Add FastAPI TestClient tests for critical API endpoints.

### 7. Extract long functions (Code Quality)
**Why:** 7 functions exceed 200 lines. High coupling makes testing and modification risky.
**Action:** Decompose `assess_market()`, `_live_fill()`, `_assess_single_market()`, `calculate_position_size()`, `_run_migrations()`, and `scan_and_trade()`.

### 8. Implement exponential backoff in Kalshi circuit breaker (Reliability)
**Why:** Flat 60s halt after 5xx errors. Extended Kalshi outages cause repeated 60s retry cycles.
**Action:** Exponential backoff: 60s, 120s, 240s, 480s, cap at 600s.

### 9. Add article minimum word count filter (Performance)
**Why:** Paywalled/empty articles included in Claude's context degrade forecast quality.
**Action:** Reject articles with <50 extracted words in `news_researcher.py`.

### 10. Expand RSS feed coverage (Performance)
**Why:** Only 3 feeds (Reuters top/business, NYT Politics). Missing economics, geopolitics, tech/AI domains.
**Action:** Add NYT Business, World, Science feeds and AP News top stories.

---

*Report generated by Claude Opus 4.6 on March 30, 2026. 931 tests passing. All findings verified against source code.*
