# PolyEdge — Complete Codebase Audit Report

**Date:** March 28, 2026
**Auditor:** Claude Opus 4.6 (automated)
**Codebase:** `/Users/adamgrodin/polyedge`
**Commit:** `dc93686` (main)
**Scope:** All 12 audit sections per POLYEDGE-AUDIT-PROMPT.md

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Source files** | 63 Python files in `src/` |
| **Test files** | 65 Python files in `tests/` |
| **Total source LOC** | 15,072 |
| **Total test LOC** | 12,352 |
| **Total LOC** | 27,424 |
| **Tests collected** | 837 |
| **Tests passing** | 834 (3 skipped, 0 failed) |
| **External API integrations** | 10 (Kalshi, Polymarket, Anthropic, Serper, FRED, FedWatch, Cleveland Fed, Metaculus, Manifold, DuckDuckGo) |
| **Env vars (total)** | 9 (3 required, 6 optional) |
| **Env vars (documented)** | 9/9 — all documented in `.env.example` |
| **Dependencies (pinned)** | 17 exact-version pins |
| **Dependencies (optional)** | 5 (commented, unpinned) |

---

## Issues by Severity

### 🔴 CRITICAL — Fix Before Next Trade

**C-1. Polymarket integration has no US residency safeguard**
- **Files:** `src/main.py:869-900`, `src/core/polymarket_client.py:131-172`, `src/execution/order_router.py:364-467`
- **What's wrong:** The codebase can place real orders on Polymarket without verifying the user is not a US resident. Polymarket is not legal for US persons. Three safety gates exist (config mode, env var, interactive prompt) but none check jurisdiction.
- **Impact:** Regulatory exposure. CFTC enforcement risk if live Polymarket trades are placed from a US IP/wallet.
- **Fix:** Add a required `CONFIRM_NON_US_POLYMARKET=true` env var gate before any Polymarket order executes. Add legal disclaimer in README. Consider disabling Polymarket by default in settings.yaml (currently `enabled: false`, which is good — ensure it stays that way).

**C-2. Floating-point arithmetic for all monetary values**
- **Files:** `src/storage/database.py:23-27` (schema), `src/execution/position_manager.py` (P&L), `src/execution/order_router.py` (fees)
- **What's wrong:** All prices, fees, and P&L values stored as `REAL` (float) in SQLite and computed with Python `float`. The schema has an explicit comment acknowledging this: "Ideally these would be INTEGER cents... migrating the schema is deferred to avoid risk."
- **Impact:** Rounding errors accumulate over hundreds of trades. At $5K bankroll with ~100 trades/month, cumulative error is likely sub-dollar but could cause incorrect risk checks or P&L reporting.
- **Fix:** Short-term: acceptable as-is since `round(pnl, 4)` is used and all comparisons should use epsilon tolerance. Medium-term: migrate to integer cents for database storage.

**C-3. Race condition in trade deduplication during pm2 restarts**
- **Files:** `src/main.py:479-486`
- **What's wrong:** Database-level dedup checks for recent trades, but if pm2 restarts and two instances overlap (old dying, new starting), both could pass the check-then-write sequence.
- **Impact:** Duplicate live orders worth real money.
- **Fix:** Use SQLite `INSERT OR IGNORE` with a unique constraint on `(market_id, strategy, direction, timestamp_bucket)` or a file-based PID lock to prevent concurrent execution.

**C-4. Order cancellation uses internal ID, not Kalshi's order_id**
- **Files:** `src/execution/order_router.py:572`
- **What's wrong:** `cancel_order()` passes the internal `PE-xxx` order ID to `self.kalshi.cancel_order()`, but Kalshi expects their own order UUID from the create_order response.
- **Impact:** Order cancellation silently fails. Stale limit orders remain resting on exchange.
- **Fix:** Store Kalshi's `order_id` from the create_order response in the orders table. Use that for cancel operations.

**C-5. No superforecaster-style decomposition in prompts**
- **Files:** `src/analysis/prompt_templates.py:37-159`
- **What's wrong:** Prompts ask Claude to consider factors but never require structured decomposition of compound questions (e.g., P(X and Y) = P(X) × P(Y|X)). Research shows decomposition significantly improves calibration.
- **Impact:** Systematically inaccurate forecasts on compound-probability markets, leading to false-positive edge signals.
- **Fix:** Add explicit decomposition instruction to each template: "Break this question into independent sub-questions with individual probabilities. Combine using multiplication for AND, addition for OR."

**C-6. Full article text never fetched from news sources**
- **Files:** `src/analysis/news_researcher.py:135-245`
- **What's wrong:** Only headlines + 2-3 sentence snippets are fetched from DuckDuckGo and Serper. Claude makes probability estimates based on headlines, not full article content.
- **Impact:** For Fed/macro and policy markets, missing detail in article bodies degrades forecast accuracy. A headline like "Fed Signals Rate Cut" without the CPI data context could lead to incorrect probability.
- **Fix:** Add optional full-article fetching for top 2-3 results using `httpx` + text extraction (trafilatura or readability). Truncate to ~2000 tokens per article. The FRED/FedWatch/Cleveland Fed data sources partially compensate but don't replace article detail.

---

### 🟠 HIGH — Fix This Week

**H-1. Multi-model ensemble default over-weights models vs market**
- **File:** `src/analysis/ensemble.py:106-109`
- **What's wrong:** `multi_model_ensemble()` defaults to `market_weight=0.15`, giving the market only 15% weight. The market represents actual capital allocation by all participants and should carry more weight than community forecasts from Metaculus/Manifold.
- **Impact:** Over-trusting model consensus on tail events → false-positive signals → bad trades.
- **Fix:** Change default to `market_weight=0.40` or make it a config parameter. The single-model ensemble at `claude_weight=0.85` is separately calibrated and acceptable.

**H-2. No calibration feedback in system prompt**
- **File:** `src/analysis/prompt_templates.py:20-34`
- **What's wrong:** The system prompt tells Claude to calibrate but provides no examples, no feedback on past performance, and no explicit anti-overconfidence instruction. Claude doesn't know its own track record.
- **Impact:** Wider-than-necessary confidence intervals reduce signal count. Potential systematic over/under-confidence.
- **Fix:** Add to SYSTEM_PROMPT: calibration rules ("95%+ probabilities are rarely justified"), and optionally inject rolling Brier score data ("Your recent 70% predictions resolved YES 68% of the time").

**H-3. Missing response field validation in live order flow**
- **File:** `src/execution/order_router.py:277-289`
- **What's wrong:** After order creation, the code validates `order_id` exists but doesn't validate other critical fields (`avg_price`, `filled_count`, `status`) before using them.
- **Impact:** Malformed API response could cause silent NaN calculations or incorrect position tracking.
- **Fix:** Add field validation: `if not all(k in result for k in ("order_id", "status")): reject`.

**H-4. No log rotation configured**
- **File:** `src/main.py:57-75`
- **What's wrong:** Logs written to `data/logs/polyedge.log` via `FileHandler` with no rotation. Running 24/7, this file grows unbounded.
- **Impact:** Disk space exhaustion on Mac Mini after weeks/months of operation.
- **Fix:** Replace `FileHandler` with `RotatingFileHandler(maxBytes=10*1024*1024, backupCount=5)` for 10MB rotation with 5 backups.

**H-5. scan_and_trade() is 568 lines — monolithic function**
- **File:** `src/main.py:82-649`
- **What's wrong:** Core orchestration function is 568 lines with 6+ levels of nesting. Market price fetching logic is repeated 3 times. Exit processing is deeply nested.
- **Impact:** Hard to debug, test, or modify without introducing regressions.
- **Fix:** Extract sub-functions: `_fetch_market_prices()`, `_process_exits()`, `_sort_and_execute_signals()`, `_run_strategies()`.

**H-6. WebSocket auth failure detection via string matching**
- **File:** `src/core/websocket_client.py:212-218`
- **What's wrong:** Authentication failure detection relies on substring matching ("401", "403", "authentication", "unauthorized") which could miss auth errors with different messages.
- **Impact:** Permanent auth failures retried indefinitely instead of failing fast.
- **Fix:** Check HTTP status codes directly when available, fall back to string matching.

**H-7. Cross-check feature disabled by default and overly strict**
- **File:** `src/analysis/claude_forecaster.py:361-474`, `src/config.py:106`
- **What's wrong:** `cross_check_enabled` defaults to `False`. When enabled, markets with >22% disagreement between two temperature runs are SKIPPED entirely (returning None), which is overly conservative and burns 2x Claude API budget.
- **Impact:** Underutilized validation mechanism. When on, it over-filters.
- **Fix:** Default to enabled. On disagreement, downgrade confidence instead of skipping.

---

### 🟡 MEDIUM — Fix When Possible

**M-1. Database uses REAL for prices instead of INTEGER cents**
- **File:** `src/storage/database.py:23-27`
- **What's wrong:** Documented technical debt. All monetary values stored as float.
- **Impact:** Sub-cent rounding errors over long periods. Epsilon comparisons required everywhere.
- **Fix:** Plan schema migration to INTEGER cents. Add migration script.

**M-2. Magic numbers not in config**
- **Files:** `src/execution/position_manager.py:18-24`, `src/core/websocket_client.py:29-31`, `src/core/kalshi_client.py:33-34`, `src/execution/fill_tracker.py:22`
- **What's wrong:** Several operational constants are hardcoded:
  - `DEFAULT_STOP_LOSS_PCT = 0.30` (position_manager.py:18)
  - `DEFAULT_MAX_HOLD_DAYS = 21` (position_manager.py:19)
  - `DEFAULT_EDGE_GONE_THRESHOLD = 0.20` (position_manager.py:20)
  - `INITIAL_BACKOFF = 1.0`, `MAX_BACKOFF = 60.0` (websocket_client.py:29-30)
  - `max_concurrent = 5` (kalshi_client.py:33)
  - `max_retries = 3` (kalshi_client.py:153)
  - `MAX_POLLS = 5` (fill_tracker.py:22)
- **Impact:** Cannot tune without code changes.
- **Fix:** Move to `settings.yaml` under appropriate sections.

**M-3. Foreign key enforcement disabled in database**
- **File:** `src/storage/database.py:249-259`
- **What's wrong:** FK constraints off due to incomplete schema migration.
- **Impact:** Orphaned records possible if markets deleted without cascading deletes.
- **Fix:** Enable FKs after verifying all foreign key relationships are correct.

**M-4. Temperature not populated per category**
- **File:** `src/analysis/claude_forecaster.py:75-79`, `src/config.py:105`
- **What's wrong:** `category_temperatures` dict defaults to empty. All categories use 0.3.
- **Impact:** Missed optimization — Politics could use lower temp for consistency, Culture could use higher for creative reasoning.
- **Fix:** Populate defaults: `{"Politics": 0.25, "Fed": 0.20, "Culture": 0.40, "Geopolitics": 0.30}`.

**M-5. News relevance scoring is keyword-only**
- **File:** `src/analysis/news_researcher.py:280-309`
- **What's wrong:** Relevance is scored by keyword overlap + recency bonus. No semantic validation. Old articles with matching keywords score equally to breaking news.
- **Impact:** Stale or tangentially relevant context fed to Claude, degrading forecast quality.
- **Fix:** Add absolute date validation (reject >7 days for Fed, >30 days for Culture). Consider semantic similarity scoring.

**M-6. URL deduplication fragile**
- **File:** `src/analysis/news_researcher.py:394-405`
- **What's wrong:** URL normalization strips all query params. Different articles on the same domain/path with different query params treated as duplicates.
- **Impact:** Minor — most news sites don't use query params for article identity.
- **Fix:** Strip known tracking params (`utm_*`, `fbclid`, `gclid`) instead of all params.

**M-7. Resolution tracker depends on API staleness**
- **File:** `src/analysis/resolution_tracker.py:28-74`
- **What's wrong:** Market resolution requires API to return `status=settled`. No fallback to blockchain for Polymarket.
- **Impact:** Calibration Brier scores lag if API is slow to reflect settlement.
- **Fix:** Increase poll frequency near expected resolution dates. Consider blockchain fallback for Polymarket.

**M-8. Metrics kept in memory only**
- **File:** `src/metrics.py`
- **What's wrong:** Metrics (strategy performance, API latency, etc.) are in-memory and lost on restart.
- **Impact:** No historical data for performance analysis across restarts.
- **Fix:** Persist key metrics to database or export to a file on shutdown.

**M-9. Gate 3 confirmation logic duplicated**
- **Files:** `src/execution/order_router.py:226-245` and `379-395`
- **What's wrong:** `_live_fill()` and `_poly_live_fill()` have nearly identical TTL check and re-prompt logic.
- **Impact:** Maintenance burden; changes must be made in two places.
- **Fix:** Extract to shared `_check_gate3_confirmation()` method.

**M-10. Silent failure when all strategies produce zero signals**
- **File:** `src/main.py:387-426`
- **What's wrong:** Each strategy is wrapped in try/except that logs and continues. If ALL strategies fail simultaneously, the result is an empty signal list with no special alert.
- **Impact:** Systemic issues (bad config, API down) masked by per-strategy error handling.
- **Fix:** Add check: if all strategies raised exceptions, send alert.

---

### 🟢 LOW — Optional

**L-1. Orphaned module: `src/data/leaderboard.py`**
- Not imported by any active code. Superseded by `whale_monitor.py`.
- **Fix:** Remove or move to `scripts/`.

**L-2. `print()` in package module**
- **File:** `src/scripts/calibration_report.py:124`
- **Fix:** Replace with `logger.info()`.

**L-3. PM2 log file permissions**
- **File:** `ecosystem.config.js:20-21`
- Logs at `~/.pm2/logs/polyedge-*.log` could contain sensitive startup output.
- **Fix:** Verify `chmod 600 ~/.pm2/logs/polyedge-*.log`.

**L-4. Dashboard port hardcoded in function signature**
- **File:** `src/dashboard/server.py:44`
- Port defaults to 8080; overridable at call site but not in config.
- **Fix:** Add `dashboard_port` to settings.yaml.

**L-5. `database.py` is 1463 lines — monolithic**
- **File:** `src/storage/database.py`
- Schema, migrations, and 50+ query methods in one class.
- **Fix:** Extract schema to separate file, migrations to separate module.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | ✅ RSA-PSS | ✅ All endpoints | ✅ 3x exponential+jitter | ✅ Semaphore(5)+0.1s interval | ✅ 60s configurable | ✅ Comprehensive | **Healthy** |
| Kalshi WebSocket | ✅ RSA-PSS | ✅ Reconnect | ✅ Exp backoff 1-60s | ✅ Built-in | ✅ Configurable | ✅ Good | **Healthy** |
| Anthropic (Claude) | ✅ API key | ✅ Timeout+retry | ✅ 3x exponential | ✅ Token budget tracking | ✅ 60s asyncio timeout | ✅ Mocked | **Healthy** |
| Serper (Search) | ✅ API key | ✅ Returns empty on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| FRED | ✅ API key | ✅ Returns None on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| FedWatch | 🔓 Public | ✅ Returns None on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| Cleveland Fed | 🔓 Public | ✅ Returns None on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| Metaculus | ✅ API token | ✅ Returns None on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| Manifold | 🔓 Public | ✅ Returns None on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ httpx default | ✅ Mocked | **Adequate** |
| DuckDuckGo | 🔓 Public | ✅ Returns empty on fail | ⚠️ No explicit retry | ⚠️ Not explicit | ✅ ddgs default | ✅ Mocked | **Adequate** |

**Note:** Secondary data sources (FRED, FedWatch, etc.) degrade gracefully — missing data is simply omitted from Claude's context. Retry logic is less critical for these.

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | ✅ Kalshi + Polymarket + Gamma API with pagination | ✅ Mocked | ✅ Volume/liquidity/category filters | **Solid** |
| Forecast Generation | ✅ Claude with category-specific prompts, ensemble | ✅ 12+ tests | ⚠️ No decomposition (C-5), no calibration feedback (H-2) | **Good, needs tuning** |
| Edge Detection | ✅ `claude_prob - market_price` with min threshold | ✅ Tested | ✅ Min edge 5% (AI), 2% (arb) | **Solid** |
| Position Sizing | ✅ Half-Kelly with caps, calibration multiplier | ✅ Tested | ✅ 5% per position, 40% total, 20% correlated | **Solid** |
| Order Execution | ✅ Paper + live modes, maker preference | ✅ Tested | ✅ Three-gate safety, balance checks | **Solid** |
| Position Tracking | ✅ Weighted avg entry, proportional fees, partial fills | ✅ Tested | ✅ Crash recovery via DB | **Solid** |
| P&L Calculation | ✅ Realized + unrealized, includes all fees | ✅ Tested | ⚠️ Float precision (C-2) | **Good** |
| Settlement Handling | ✅ Polls Kalshi/Polymarket for resolved markets | ✅ Tested | ✅ Rejects intermediate prices | **Solid** |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|---|---|---|---|---|---|---|
| `src/core/kalshi_client.py` | 5 | 5 | 5 | 5 | 4 | **5/5** |
| `src/core/market_discovery.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/core/models.py` | 5 | 5 | 5 | N/A | 4 | **5/5** |
| `src/core/websocket_client.py` | 4 | 4 | 4 | 3 | 3 | **4/5** |
| `src/core/polymarket_client.py` | 4 | 4 | 4 | 3 | 3 | **4/5** |
| `src/analysis/claude_forecaster.py` | 4 | 5 | 5 | 4 | 4 | **4/5** |
| `src/analysis/prompt_templates.py` | 3 | 4 | N/A | 4 | 3 | **3/5** |
| `src/analysis/ensemble.py` | 4 | 5 | 4 | 4 | 4 | **4/5** |
| `src/analysis/calibration.py` | 5 | 5 | 4 | N/A | 4 | **5/5** |
| `src/analysis/calibration_analyzer.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/analysis/news_researcher.py` | 3 | 4 | 4 | 3 | 3 | **3/5** |
| `src/analysis/resolution_tracker.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/strategies/ai_probability.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/strategies/obvious_no.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/strategies/cross_arb.py` | 4 | 4 | 4 | 4 | 3 | **4/5** |
| `src/strategies/cross_platform_arb.py` | 4 | 4 | 4 | 3 | 3 | **4/5** |
| `src/strategies/news_reactive.py` | 4 | 4 | 4 | 4 | 3 | **4/5** |
| `src/strategies/whale_tracker.py` | 4 | 4 | 4 | 4 | 3 | **4/5** |
| `src/data/market_scanner.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/data/market_graph.py` | 4 | 4 | 3 | N/A | 3 | **4/5** |
| `src/data/data_enricher.py` | 5 | 4 | 5 | N/A | 4 | **5/5** |
| `src/data/news_ingestion.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/data/whale_monitor.py` | 4 | 4 | 4 | 4 | 3 | **4/5** |
| `src/data/fred_client.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/data/fedwatch.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/data/cleveland_fed.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/data/manifold_client.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/data/metaculus_client.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/execution/order_builder.py` | 4 | 4 | 4 | 4 | 4 | **4/5** |
| `src/execution/order_router.py` | 3 | 4 | 4 | 5 | 3 | **4/5** |
| `src/execution/position_manager.py` | 4 | 4 | 4 | 4 | 3 | **4/5** |
| `src/execution/fill_tracker.py` | 5 | 4 | 5 | 4 | 4 | **5/5** |
| `src/risk/risk_engine.py` | 5 | 5 | 5 | 5 | 4 | **5/5** |
| `src/risk/kelly_sizer.py` | 5 | 5 | 4 | 5 | 4 | **5/5** |
| `src/risk/circuit_breaker.py` | 5 | 5 | 4 | 5 | 4 | **5/5** |
| `src/risk/portfolio_risk.py` | 4 | 4 | 4 | 5 | 3 | **4/5** |
| `src/alerts/alert_manager.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/alerts/daily_report.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/alerts/imessage_alert.py` | 4 | 4 | 4 | N/A | 3 | **4/5** |
| `src/dashboard/server.py` | 4 | 3 | 3 | N/A | 3 | **3/5** |
| `src/storage/database.py` | 3 | 4 | 4 | 3 | 4 | **3/5** |
| `src/main.py` | 3 | 4 | 4 | 5 | 4 | **4/5** |
| `src/config.py` | 5 | 5 | 4 | N/A | 5 | **5/5** |
| `src/metrics.py` | 3 | 3 | 3 | N/A | 3 | **3/5** |

---

## Section 1: Structural Integrity

### Directory Tree Summary
```
polyedge/
├── config/                     # 4 files: settings.yaml, categories.yaml, .env, .env.example
├── src/                        # 63 Python files
│   ├── core/          (7)      # Models, clients (Kalshi, Polymarket), WebSocket, market discovery
│   ├── analysis/      (9)      # Claude forecaster, prompts, ensemble, calibration, news research
│   ├── strategies/    (7)      # AI probability, obvious NO, cross-arb, cross-platform, news, whales
│   ├── data/          (15)     # Scanners, graph, cache, news, whale monitor, FRED, FedWatch, etc.
│   ├── execution/     (5)      # Order builder, router, position manager, fill tracker, exit logic
│   ├── risk/          (5)      # Risk engine, Kelly sizer, circuit breaker, portfolio risk
│   ├── alerts/        (4)      # Alert manager, iMessage, daily report
│   ├── dashboard/     (2+7)    # FastAPI server, 6 HTML templates, 1 CSS
│   ├── storage/       (1)      # SQLite database with WAL
│   ├── scripts/       (2)      # Backtest, calibration report
│   └── root           (4)      # main.py, config.py, metrics.py, __init__.py
├── tests/                      # 65 test files (mirrors src/ structure)
├── scripts/                    # 4 CLI scripts (backfill, backtest, discover whales, etc.)
├── data/                       # SQLite DBs, ChromaDB, logs (all gitignored)
├── ecosystem.config.js         # PM2 process manager config
├── requirements.txt            # 17 pinned dependencies
├── pyproject.toml              # Build config, pytest settings
├── Makefile                    # test, run, lint, clean, install, start, stop, logs
└── .gitignore                  # Comprehensive (secrets, data, caches, IDE)
```

### Orphaned Files
- `src/data/leaderboard.py` — not imported by any active code. Superseded by `whale_monitor.py`.

### Dead Code
- None found. All exported symbols are imported elsewhere or used in tests.

### Config File Status
| File | Present | Valid |
|------|---------|-------|
| `ecosystem.config.js` | ✅ | ✅ Correct PM2 config |
| `config/.env` | ✅ | ✅ Permissions 0o600 |
| `config/.env.example` | ✅ | ✅ All 9 vars documented |
| `config/settings.yaml` | ✅ | ✅ 101 lines, well-commented |
| `config/categories.yaml` | ✅ | ✅ 51 lines |
| `requirements.txt` | ✅ | ✅ 17 exact pins |
| `pyproject.toml` | ✅ | ✅ Python >=3.12 |

### Dependency Pinning
All 17 production dependencies pinned to exact versions. 5 optional dependencies (chromadb, sentence-transformers, apscheduler, pandas, numpy) are commented out and unpinned — acceptable since they're not installed.

### Unused Dependencies
None identified. All 17 pinned packages are actively imported.

### Known CVEs
No known critical CVEs for the pinned versions as of March 2026. `cryptography==46.0.5` is recent and actively maintained.

---

## Section 2: Configuration & Environment

### Complete Environment Variable Inventory
| Variable | Required | Documented | Used In |
|----------|----------|------------|---------|
| `KALSHI_API_KEY_ID` | Yes | ✅ | `src/config.py:237` |
| `KALSHI_PRIVATE_KEY_PATH` | Yes (if Kalshi) | ✅ | `src/config.py:238` |
| `ANTHROPIC_API_KEY` | Yes | ✅ | `src/config.py:239` |
| `SERPER_API_KEY` | No | ✅ | `src/config.py:240` |
| `SEARXNG_URL` | No | ✅ | `src/config.py:241` |
| `FRED_API_KEY` | No | ✅ | `src/config.py:242` |
| `METACULUS_API_TOKEN` | No | ✅ | `src/config.py:243` |
| `POLYMARKET_PRIVATE_KEY` | No | ✅ | `src/config.py:244` |
| `POLYEDGE_LIVE_ENABLED` | Safety gate | ✅ | `src/config.py:245`, `src/main.py:808,813` |

**Undocumented env vars:** 0
**Hardcoded secrets:** 0 found
**Secrets in git history:** 0 found

### Security Posture
- `.env` file: permissions 0o600 ✅
- `kalshi_private_key.pem`: permissions 0o600, gitignored ✅
- `.gitignore` covers: `.env`, `*.pem`, `*.key`, `credentials*.json` ✅
- All API endpoints configurable via `settings.yaml` ✅
- Demo/production toggle: `kalshi.use_demo` in settings.yaml ✅
- Paper/live toggle: `trading.mode` in settings.yaml + env var gate ✅

---

## Section 3: Kalshi Integration

### Endpoints Used
| Endpoint | Method | File | Auth |
|----------|--------|------|------|
| `GET /exchange/status` | `health_check()` | `kalshi_client.py:205` | No |
| `GET /markets` | `get_markets()` | `kalshi_client.py:218` | No |
| `GET /markets/{ticker}` | `get_market()` | `kalshi_client.py:240` | No |
| `GET /events` | `get_events()` | `kalshi_client.py:251` | No |
| `GET /markets/{ticker}/orderbook` | `get_orderbook()` | `kalshi_client.py:270` | No |
| `GET /markets/trades` | `get_market_history()` | `kalshi_client.py:281` | No |
| `GET /portfolio/balance` | `get_balance()` | `kalshi_client.py:317` | ✅ RSA-PSS |
| `GET /portfolio/positions` | `get_positions()` | `kalshi_client.py:336` | ✅ RSA-PSS |
| `POST /portfolio/orders` | `create_order()` | `kalshi_client.py:354` | ✅ RSA-PSS |
| `DELETE /portfolio/orders/{id}` | `cancel_order()` | `kalshi_client.py:394` | ✅ RSA-PSS |
| `GET /portfolio/orders/{id}` | `get_order()` | `kalshi_client.py:407` | ✅ RSA-PSS |
| `GET /portfolio/orders` | `get_open_orders()` | `kalshi_client.py:421` | ✅ RSA-PSS |

### Authentication
- RSA-PSS signing with SHA-256 ✅
- Millisecond timestamps ✅
- Private key file permission check + auto-fix ✅
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` ✅

### Rate Limiting
- Semaphore limits to 5 concurrent requests ✅
- 0.1s minimum interval between requests ✅
- 429 handler: exponential backoff (2^n + jitter), 3 retries ✅

### Order Placement
- Price conversion: dollars → integer cents via `int(round(dollars * 100))` ✅
- NO-side: `yes_price = cents(1.0 - order.price)` ✅
- Quantity: `int(order.size)` ✅
- Order types: "limit" (GTC) or "market" (FOK) ✅
- Response validation: checks `order_id` exists before proceeding ✅
- Fill polling: up to 5 polls with 2s delay ✅

### Monetary Calculations
- **Prices:** Float in Python, converted to integer cents at API boundary ✅
- **Fees:** `kalshi_taker_fee()` returns integer cents, uses `math.ceil` ✅
- **P&L:** `round(realized_pnl, 4)` — 4 decimal precision ✅
- **Storage:** REAL (float) in SQLite — documented tech debt ⚠️ (C-2)

### Partial Fill Handling
- Delta tracking: records only NEW contracts per poll, not cumulative ✅
- Crash recovery: loads partial fill counts from trades table on restart ✅
- Non-monotonic detection: warns if filled_count decreases ✅

### Settlement Handling
- Polls for `status=settled` and parses `result` field ✅
- Rejects intermediate prices (0.25-0.75 on Polymarket) as incomplete ✅
- Updates calibration records on resolution ✅

---

## Section 4: AI Forecasting Pipeline

### Prompt Engineering
- 6 category-specific templates (Politics, Fed, Geopolitics, Tech, Culture, General) ✅
- Market price included in all prompts: `CURRENT MARKET PRICE: {market_price:.0%}` ✅
- Resolution criteria included verbatim ✅
- Base rate instructions present ✅
- Prompt injection sanitization with 7+ patterns stripped ✅
- Input length limits enforced (question: 500, criteria: 2000, news: 5000 chars) ✅
- **Missing:** Superforecaster-style decomposition ❌ (C-5)
- **Missing:** Calibration feedback in system prompt ❌ (H-2)

### Response Parsing
- 4-stage fallback: JSON → markdown block → brace extraction → regex ✅
- Takes LAST probability match (avoids stale references) ✅
- Clamps to [0.01, 0.99] ✅
- Inverted CI auto-correction ✅

### Model Selection
- Sonnet for routine, Opus for positions > $50 threshold ✅
- Configurable threshold ✅

### Token & Cost Tracking
- Per-model pricing tracked (Sonnet $3/$15M, Opus $15/$60M) ✅
- Daily budget warning at 500K tokens ✅
- Daily counter reset ✅

### Ensemble
- Single-model: `claude_weight=0.85` with CI penalty and extreme-price adjustment ✅
- Multi-model: Brier-score-weighted average with disagreement factor ✅
- **Issue:** Multi-model `market_weight=0.15` is too low (H-1)

### GPT-4o Integration
- **Not implemented.** System uses Claude only + community forecast cross-reference (Metaculus, Manifold). No OpenAI integration.

---

## Section 5: Data Pipeline & News Integration

### Search Sources
- DuckDuckGo (ddgs): primary, no API key needed ✅
- Serper: optional, API key required ✅
- RSS feeds: Reuters, NYT Politics ✅

### Query Generation
- 4 query variants: core topic, time-scoped, entity-focused, broad context ✅

### Data Freshness
- News items scored by recency + keyword relevance ✅
- Title-based deduplication (Jaccard > 0.7) ✅
- **Issue:** No absolute date validation — old articles with matching keywords score well (M-5)
- **Issue:** Only headlines + snippets fetched, never full text (C-6)

### Caching
- TTL cache implemented in `src/data/cache.py` ✅
- Stale data served during refresh failures ✅

### Data Enrichment Pipeline (data_enricher.py)
- Concurrent fetching with `asyncio.gather` ✅
- 15-second hard timeout ✅
- Pending task cancellation on timeout ✅
- Priority-based section truncation ✅
- Graceful degradation: missing sources simply omitted ✅

---

## Section 6: Trading Logic & Risk Management

### Edge Detection
- AI strategy: `min_edge_ai = 0.05` (5% minimum) ✅
- Arbitrage: `min_edge_arb = 0.02` (2% minimum) ✅
- Edge = `claude_probability - market_price` ✅

### Position Sizing
- Half-Kelly: `f * 0.5 * bankroll` ✅
- Hard cap: 5% per position ✅
- Calibration multiplier adjusts Kelly based on Brier score ✅
- Contracts rounded to integer ✅

### Risk Checks (10-point gate)
1. Balance check (includes pending orders) ✅
2. Position size limit (5% bankroll) ✅
3. Total exposure limit (40% bankroll) ✅
4. Correlated exposure limit (20% bankroll) ✅
5. Daily loss limit (10% → circuit breaker) ✅
6. Market liquidity check ✅
7. Existing position check (no double-entry) ✅
8. Edge minimum check ✅
9. Resolution date check ✅
10. Cooldown check ✅

### Circuit Breaker
- Daily loss > 10% bankroll → halt all trading ✅
- 3 consecutive losing days → quarter-Kelly ✅
- 5 consecutive losing days → halt, require manual review ✅
- Unrealized losses weighted at 30% for daily P&L ✅

### Stop Loss
- 30% loss of cost basis → auto-exit ✅
- Max hold 21 days ✅
- Edge-gone threshold 20% ✅

### Market Manipulation
- No wash trading or spoofing patterns detected ✅
- Maker orders preferred ✅
- Order sizes checked against book depth ✅

---

## Section 7: Backtesting & Performance Tracking

### Backtest Infrastructure
- `scripts/run_backtest.py` and `scripts/backtest_engine.py` present ✅
- Uses resolved Kalshi markets for historical validation ✅

### Calibration Tracking
- Brier score calculation: `mean((predicted - actual)^2)` ✅
- 10-bin calibration curves (predicted vs actual resolution rate) ✅
- Per-category accuracy breakdown ✅
- Per-probability-bucket analysis ✅
- Min sample sizes enforced (5 for reporting, 4 for base rates) ✅

### Decision Logging
- Every forecast logged: market_id, predicted_prob, market_price, timestamp, model_used ✅
- Every trade logged: entry price, exit price, P&L, fees, strategy ✅
- Signals logged with reasoning and edge size ✅
- Raw Claude prompts + responses logged at DEBUG level ✅

### Selection Bias
- No cherry-picking detected. All predictions logged regardless of outcome.

---

## Section 8: Error Handling & Reliability

### Try/Except Coverage
- All external API calls wrapped in try/except ✅
- Pattern: `except (httpx.HTTPStatusError, httpx.RequestError)` + `except Exception` ✅
- **No bare `except:` clauses** found ✅
- All exceptions logged at ERROR with `exc_info=True` ✅

### Retry Logic
| API | Retries | Backoff | Tested |
|-----|---------|---------|--------|
| Kalshi REST | 3 | Exponential + jitter | ✅ |
| Claude API | 3 | Exponential | ✅ |
| Kalshi WebSocket | Unlimited (reconnect) | 1-60s exponential | ✅ |
| Secondary data APIs | 0 | N/A (graceful skip) | ✅ |

### Graceful Degradation
- Anthropic down → returns market price as fallback, `parse_failed=True` ✅
- Kalshi down → scanner warns, skips cycle ✅
- Serper/FRED/etc. down → omitted from context, no crash ✅
- Internet drop mid-trade → order may be placed but fill tracking resumes on reconnect ⚠️

### State Persistence
- Positions stored in SQLite, recovered on restart ✅
- Partial fill counts loaded from trades table on restart ✅
- Circuit breaker state persisted via daily P&L in DB ✅

### Memory Leak Risk
- WebSocket callbacks stored in dicts; `remove_callback()` exists but must be called ⚠️
- Fill tracker `_partial_recorded` dict grows per order; cleaned on fill completion ✅
- No unbounded list growth detected ✅

---

## Section 9: Security Review

| Check | Status |
|-------|--------|
| No credentials in code | ✅ Grep found 0 matches for `sk-ant`, `api_key=`, `secret=`, `password=` |
| .gitignore covers secrets | ✅ `.env`, `*.pem`, `*.key`, `credentials*` |
| API keys in env vars only | ✅ All 9 vars loaded from `.env` |
| No sensitive data in logs | ✅ API responses not logged at INFO; DEBUG only |
| HTTPS for all API calls | ✅ All endpoints use `https://` |
| No command injection | ✅ No `subprocess` calls found |
| No SQL injection | ✅ All queries use parameterized `?` placeholders |
| File permissions | ✅ `.env` and `.pem` at 0o600 |

---

## Section 10: Code Quality

| Check | Status | Details |
|-------|--------|---------|
| Functions > 50 lines | ⚠️ | `scan_and_trade()` is 568 lines (H-5) |
| Files > 300 lines | ⚠️ | `database.py` (1463), `main.py` (~1020), `order_router.py` (~580), `position_manager.py` (~598), `claude_forecaster.py` (571) |
| TODO/FIXME/HACK/XXX | ✅ | 0 found in source |
| Bare except clauses | ✅ | 0 found |
| Mutable default arguments | ✅ | 0 found |
| Type hints | ✅ | All function signatures have type hints |
| f-string consistency | ✅ | f-strings used throughout |
| Magic numbers | ⚠️ | 10+ operational constants hardcoded (M-2) |
| Docstrings | ✅ | All public functions have docstrings |
| Log levels | ✅ | Proper DEBUG/INFO/WARNING/ERROR usage |
| print() statements | ✅ | 1 in `src/scripts/` (L-2), rest in `scripts/` (CLI, acceptable) |
| Copy-pasted code | ⚠️ | Gate 3 logic duplicated (M-9), market price fetching 3x (H-5) |
| Import organization | ✅ | stdlib → third-party → local pattern followed |

---

## Section 11: Regulatory Compliance

| Check | Status | Details |
|-------|--------|---------|
| Primary platform is Kalshi (CFTC-regulated) | ✅ | Kalshi is the primary trading target |
| No illegal Polymarket trading for US | ⚠️ | Polymarket integration exists, no residency check (C-1). Currently disabled in config (`polymarket.enabled: false`). |
| Terms of service compliance | ✅ | No TOS violations detected. Maker orders, standard API usage. |
| Position limits | ✅ | 5% per position, 40% total, well within Kalshi limits |
| No market manipulation | ✅ | No wash trading, spoofing, or price manipulation patterns |
| Trade record-keeping | ✅ | All trades logged with timestamps, prices, P&L in SQLite |

---

## Section 12: Improvement Roadmap Audit

| Feature | Status | Notes |
|---------|--------|-------|
| Feeding Kalshi market price into Claude's prompt | ✅ Implemented | `CURRENT MARKET PRICE: {market_price:.0%}` in all templates |
| GPT-4o as second forecaster | ❌ Not implemented | Uses Metaculus/Manifold community forecasts instead |
| Superforecaster-style prompt decomposition | ❌ Not implemented | Prompts mention base rates but don't require structured decomposition (C-5) |
| Fetching full article text from Serper results | ❌ Not implemented | Headlines + snippets only (C-6) |
| Multi-model ensemble with disagreement handling | ✅ Implemented | Brier-weighted, disagreement factor, confidence penalty |
| Calibration tracking with Brier scores | ✅ Implemented | Per-category, per-bucket, rolling windows |
| Performance dashboard | ✅ Implemented | FastAPI at :8080, 6 pages (portfolio, signals, strategies, calibration, risk, base) |

---

## Top 10 Recommendations (Prioritized)

### 1. 🔴 Fix order cancellation to use Kalshi's order_id (C-4)
**Risk:** Stale orders left on exchange. **Effort:** 1 hour. Store Kalshi order_id in DB, use for cancel.

### 2. 🔴 Add Polymarket residency safeguard (C-1)
**Risk:** Regulatory exposure. **Effort:** 30 min. Add env var gate `CONFIRM_NON_US_POLYMARKET=true`.

### 3. 🔴 Add race condition protection for pm2 restarts (C-3)
**Risk:** Duplicate live orders. **Effort:** 1 hour. Use PID lock file or `INSERT OR IGNORE` with unique constraint.

### 4. 🔴 Add superforecaster decomposition to prompts (C-5)
**Risk:** Bad forecasts on compound events. **Effort:** 2 hours. Add decomposition instructions to each template.

### 5. 🟠 Add calibration feedback to system prompt (H-2)
**Risk:** Miscalibrated confidence → wrong position sizes. **Effort:** 1 hour. Inject rolling Brier stats and anti-overconfidence rules.

### 6. 🟠 Fix multi-model ensemble market weight (H-1)
**Risk:** Over-trusting model consensus. **Effort:** 15 min. Change default from 0.15 to 0.40+.

### 7. 🟠 Add log rotation (H-4)
**Risk:** Disk exhaustion on Mac Mini. **Effort:** 15 min. Switch to `RotatingFileHandler`.

### 8. 🟠 Validate API response fields in live order flow (H-3)
**Risk:** Malformed responses → NaN calculations. **Effort:** 30 min. Add field presence checks.

### 9. 🔴 Fetch full article text for news context (C-6)
**Risk:** Headlines-only analysis degrades forecast quality. **Effort:** 3 hours. Add `httpx` fetch + text extraction for top results.

### 10. 🟠 Refactor `scan_and_trade()` into smaller functions (H-5)
**Risk:** Regression bugs from 568-line function. **Effort:** 2 hours. Extract 4-5 sub-functions.

---

## Conclusion

PolyEdge is a **well-engineered, production-ready trading system** with comprehensive risk controls, thorough test coverage (834/837 tests passing), and proper security practices. The codebase demonstrates strong engineering discipline:

- **Strengths:** 10-point risk gate, half-Kelly sizing with calibration multiplier, three-gate live trading safety, comprehensive error handling, RSA-PSS authentication, prompt injection protection, graceful degradation for all secondary data sources, zero bare excepts, zero TODO markers, all dependencies pinned, all secrets externalized.

- **Key risks:** 6 critical issues identified, none of which are show-stoppers but all should be addressed before scaling capital. The most urgent are the order cancellation bug (C-4) and race condition (C-3) which could cause real money loss in live trading.

- **Overall grade: 8/10** — Production-ready with optimizations available. Fix the 6 critical issues to reach 9/10.

---

*Report generated by Claude Opus 4.6 on March 28, 2026. All file paths and line numbers reference commit `dc93686` on main.*
