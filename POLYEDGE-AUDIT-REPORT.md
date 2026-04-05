# PolyEdge Complete Codebase Audit Report

**Audit Date:** April 5, 2026
**Auditor:** Claude Opus 4.6 (automated comprehensive audit, 7 parallel agents)
**Codebase:** PolyEdge -- AI-Driven Prediction Market Trading Bot (Kalshi)
**Platform:** Python 3.12+ on Mac Mini M4 Pro, managed by PM2

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| **Total source files** | 120 Python files (src/ + scripts/) |
| **Total lines of code** | ~68,000 (excluding venv) |
| **Total test files** | 97+ test files |
| **Test directories** | 12 (core, analysis, data, execution, risk, storage, strategies, alerts, dashboard, orchestrator, scripts, integration) |
| **External API integrations** | 8 (Kalshi REST, Kalshi WebSocket, Anthropic Claude, OpenAI GPT-4o, Serper, FRED, Metaculus, Manifold) |
| **Strategies implemented** | 8 (AI Probability, Cross-Market Arb, Cross-Platform Arb, Whale Tracking, News-Reactive, Late Resolution, Mean Reversion, Obvious NO) |
| **Environment variables** | 12 total (7 API keys/secrets, 5 system flags) -- all documented in .env.example |
| **Dependencies** | 18 pinned (100% exact version pins) |
| **Orphaned files** | 0 |
| **Unused dependencies** | 0 |

### Architecture Layers (file counts)

| Layer | Files | Purpose |
|-------|-------|---------|
| src/core | 9 | Models, Kalshi client, WebSocket, market discovery, key loader |
| src/analysis | 25 | Claude/GPT-4o forecasters, ensemble, calibration, prompts, news research |
| src/data | 17 | Market scanner, data enricher, FRED/FedWatch, whale monitor, cache |
| src/execution | 7 | Order routing, position management, fill tracking |
| src/risk | 8 | Risk engine (15 checks), Kelly sizer, circuit breaker, manipulation detection |
| src/storage | 7 | SQLite + WAL with mixin pattern (markets, trades, calibration, risk, stats, whales) |
| src/strategies | 9 | All 8 strategies + __init__ |
| src/alerts | 4 | Alert dispatch, daily reports, iMessage |
| src/dashboard | 4 | FastAPI web UI + JSON API + HTMX partials |
| src/orchestrator | 4 | Main loop, scan cycle, trade cycle, startup |
| scripts/ | 9 | Backtest, tax export, whale discovery, parameter replay |

---

## Issues by Severity

### CRITICAL -- FIX BEFORE NEXT TRADE

**C-1: Real API Keys Exposed in config/.env**
- **File:** `config/.env`
- **What's wrong:** Actual production secrets are present on disk: `ANTHROPIC_API_KEY=sk-ant-api03-...`, `KALSHI_API_KEY_ID=60ba628c-...`, `POLYMARKET_PRIVATE_KEY=ba27f30e...`, `SERPER_API_KEY=0200de8d...`, `METACULUS_API_TOKEN=7fff25b8...`. While `.gitignore` correctly lists `config/.env`, these keys exist in the working directory and could be exposed if the repo is shared, cloned, or backed up without exclusion.
- **Impact:** Unauthorized API usage at your cost (Anthropic), unauthorized trading (Kalshi), wallet drain (Polymarket private key). Potential financial loss in the thousands.
- **Fix:** Rotate ALL exposed API keys immediately via their respective platforms. Verify with `git log --all --full-history -- config/.env` that the file was never committed. Add a pre-commit hook to block `.env` commits.

**C-2: GPT-4o Integration Incomplete -- Feature Parity Gap**
- **File:** `src/analysis/openai_forecaster.py`
- **What's wrong:** GPT-4o forecaster lacks: (1) token budget coordination with Claude's daily budget system, (2) circuit breaker tie-in with Claude, (3) prompt A/B testing support (hardcoded `enabled=False` at line 231), (4) category-specific model selection, (5) accuracy_context not threaded through prompts (line 232-246). Returns `None` on failure causing ensemble to silently skip it.
- **Impact:** Ensemble produces inconsistent results. Combined daily API cost invisible. GPT-4o silently dropped without operator awareness. Could lead to systematically biased forecasts when one model fails.
- **Fix:** Either (A) complete GPT-4o with budget coordination, variant support, and proper failure signaling, OR (B) disable it in config (`openai.enabled: false`) until ready. Do not run half-integrated in production.

---

### HIGH -- FIX THIS WEEK

**H-1: No Explicit Buying Power Tracking for Concurrent Orders**
- **File:** `src/execution/order_router.py` (lines 75-124)
- **What's wrong:** Balance pre-flight check and pending cost tracking exist, but no explicit `remaining_balance = balance - pending_cost` metric. Fast concurrent order submissions could race past the balance check.
- **Impact:** Two orders submitted near-simultaneously could both pass the balance check and over-commit capital.
- **Fix:** Compute and enforce `remaining_balance` in the order routing path, accounting for all pending (unconfirmed) orders.

**H-2: WebSocket Subscription Race Condition**
- **File:** `src/core/websocket_client.py` (lines 139-157)
- **What's wrong:** `subscribe()` and `unsubscribe()` modify `self._subscriptions` set without an async lock. If `connect()` reads subscriptions while `subscribe()` writes, data corruption is possible. Callback lists (`_price_callbacks`, `_fill_callbacks`) also unprotected during iteration in `_dispatch_message()`.
- **Impact:** Missed price updates or duplicate/dropped fill notifications during reconnection.
- **Fix:** Wrap subscription set mutations and callback list iterations in `self._ws_lock`.

**H-3: Partial Fill Handling Not Visible in Codebase**
- **Files:** `src/execution/position_manager.py`, `src/risk/kelly_sizer.py`
- **What's wrong:** Kelly sizer reduces order size when >10% of book depth, but no explicit logic for what happens when an order partially fills: retry remainder? Cancel? Accept partial? Post-fill Kelly recalculation absent.
- **Impact:** Positions could be consistently undersized vs. intended, missing profitable trades. No rebalancing after partial fills at worse-than-expected prices.
- **Fix:** Add explicit partial-fill policy (retry/cancel/accept) in order_router. Log partial fill events. Recalculate Kelly if fill price deviates >2% from expected.

**H-4: No Retry on Anthropic API Key Validation at Startup**
- **File:** `src/orchestrator/lifecycle.py` (lines 284-300)
- **What's wrong:** Single attempt to validate Anthropic API key. If Anthropic is temporarily down at bot startup, the entire AI signal pipeline is permanently disabled until manual restart.
- **Impact:** Bot runs in degraded mode (no AI strategies) after a transient Anthropic outage at startup time.
- **Fix:** Retry key validation with exponential backoff (3 attempts, 2s/4s/8s) before entering degraded mode.

**H-5: No Per-Strategy Position Limits**
- **Files:** `src/risk/risk_checks.py`, `src/risk/risk_engine.py`
- **What's wrong:** Total exposure cap is 40%, but no per-strategy cap. Bot could allocate 30% to AI Probability alone. If that strategy's model degrades, portfolio risk spikes.
- **Impact:** Over-concentration in a single strategy with degraded performance could cause outsized losses.
- **Fix:** Add configurable per-strategy exposure limits (e.g., 15% max per strategy) to risk_checks.py.

**H-6: Wash-Trade Cooldown Only at Market Level, Not Event Level**
- **File:** `src/risk/risk_checks.py` (lines 348-379)
- **What's wrong:** 4-hour cooldown prevents re-entry into the same `market_id`, but doesn't prevent rapid entry/exit on different markets within the same event.
- **Impact:** Could inadvertently create wash-trade patterns across correlated markets in the same event.
- **Fix:** Extend cooldown to `event_ticker` level, not just `market_id`.

**H-7: No Strategy-Level P&L Attribution**
- **Files:** `src/risk/circuit_breaker.py`, `src/storage/db_stats.py`
- **What's wrong:** Daily P&L is tracked in aggregate but not broken down by strategy. Cannot determine which strategy is making or losing money.
- **Impact:** Cannot auto-weight strategies by performance. A losing strategy can drag the portfolio without detection.
- **Fix:** Add per-strategy realized P&L tracking in db_stats. Feed into Kelly multiplier per strategy.

**H-8: Database Prices Stored as FLOAT**
- **File:** `src/storage/database.py`
- **What's wrong:** Prices stored as REAL (floating point) in SQLite. While all arithmetic uses Decimal, storage round-trips introduce float drift.
- **Impact:** Low impact at current trade volume (<10K), but compounding rounding errors over thousands of trades could cause P&L tracking drift.
- **Fix:** Migrate to INTEGER cents storage. Plan exists (H-6 audit note) but not yet implemented.

---

### MEDIUM -- FIX WHEN POSSIBLE

**M-1: Model Selection Missing Edge Parameter in Decomposer**
- **File:** `src/analysis/decomposer.py:185`
- **What's wrong:** `_select_model()` called without edge parameter, so decomposer always uses primary model instead of escalating to Opus for high-edge compound questions.
- **Fix:** Pass `edge=0.0` or actual edge value.

**M-2: Confidence Interval Default Uses Absolute Width for Extreme Probabilities**
- **File:** `src/analysis/forecast_parser.py:200-202`
- **What's wrong:** Default CI is +/-0.25. For probability 0.02, CI becomes [0, 0.27] -- lower bound always hits 0.0 for extreme probs.
- **Fix:** Use percentage-relative width for probs <0.05 or >0.95.

**M-3: No Post-Hoc Overconfidence Penalty**
- **File:** `src/analysis/prompt_templates.py:61-62`
- **What's wrong:** Prompt tells Claude that AI models are systematically overconfident by 5-15%, but no enforcement mechanism applies shrinkage when Claude returns >0.95.
- **Fix:** Add post-hoc Brier-weighted shrinkage or Platt calibration enforcement when extreme probabilities returned.

**M-4: Serper Auth Failure Escalation Asymmetric**
- **File:** `src/analysis/news_search.py` (lines 270-293)
- **What's wrong:** 3rd auth failure permanently disables Serper. A single transient network error misclassified as auth could cause premature disable.
- **Fix:** Use circuit breaker pattern with longer cooldown before permanent disable. Track HTTP status codes to distinguish 401 from network errors.

**M-5: News Fetch Latency Could Reach 60s**
- **File:** `src/analysis/news_fetcher.py`
- **What's wrong:** MAX_ARTICLE_FETCH=3 with 5s timeout = 15s per query. 4 queries in news_researcher = 60s worst case. Market conditions change during this window.
- **Fix:** Parallel fetch with aggregate timeout (e.g., 8s for all 3 articles). Make MAX_ARTICLE_FETCH configurable.

**M-6: Entity Extraction in Search Query Construction is Fragile**
- **File:** `src/analysis/news_researcher.py` (lines 153-167)
- **What's wrong:** Regex `[A-Z][a-z]+` misses all-caps entities (SEC, FBI, FDA), hyphenated names, and multi-word entities.
- **Fix:** Add all-caps entity pattern. Expand abbreviation expansion list to include FDA, DOT, HHS, ECB, BOE.

**M-7: Manipulation Detector Flag Expiry Too Long**
- **File:** `src/risk/manipulation_detector.py:26`
- **What's wrong:** FLAG_EXPIRY_SECONDS=1800 (30 minutes). If price move was organic (validated by volume), bot still skips market for 30 minutes.
- **Fix:** Reset flag if price stabilizes or volume surges. Add volume-weighted confirmation.

**M-8: Obvious-NO Position Cap Inconsistency with Kelly**
- **File:** `src/risk/kelly_sizer.py` (lines 182-190)
- **What's wrong:** High-probability cap is 3% for P>0.95, but obvious-NO strategy targets exactly these trades. This undersizes obvious-NO vs. lower-confidence AI_PROBABILITY trades.
- **Fix:** Separate cap logic for obvious-NO strategy (e.g., 5% when P>0.98).

**M-9: CLAUDE.md Documentation Inconsistency**
- **File:** `CLAUDE.md` (lines 7-14)
- **What's wrong:** States all strategies run on "Polymarket" but actual execution is Kalshi-only. Polymarket is read-only cross-reference.
- **Fix:** Update CLAUDE.md to state: "Strategies trade on Kalshi (CFTC-regulated). Polymarket used for read-only cross-reference only."

**M-10: Polymarket Regulatory Gate Not Enforced**
- **File:** `src/orchestrator/lifecycle.py`
- **What's wrong:** CLAUDE.md mentions `CONFIRM_NON_US_POLYMARKET=true` requirement, but this check does not exist in code. Only `settings.polymarket.enabled` config flag controls it.
- **Fix:** Add explicit env var check before initializing PolymarketDiscovery.

**M-11: Adversarial Analyzer Prompts Not Sanitized**
- **File:** `src/analysis/adversarial_analyzer.py:81-82`
- **What's wrong:** Builds prompts with `market.question[:500]` but doesn't call `_sanitize_external_text()`. Inconsistent with prompt_templates.py and decomposer.py which both sanitize.
- **Fix:** Add sanitization call for consistency.

**M-12: Kelly Sizing Details Not Logged**
- **File:** `src/orchestrator/trade_cycle.py`
- **What's wrong:** Only final contract count logged, not intermediate Kelly values (edge, probability, bankroll fraction, adjustments applied).
- **Fix:** Log Kelly sizing breakdown for retroactive audit.

**M-13: Single-Model Ensemble Missing Market Efficiency Scaling**
- **File:** `src/analysis/ensemble.py:104-197`
- **What's wrong:** `multi_model_ensemble()` uses market_efficiency parameter but single-model `ensemble_forecast()` does not.
- **Fix:** Apply efficiency scaling consistently in both paths.

**M-14: No Cached Market Fallback When Kalshi Is Down**
- **File:** `src/orchestrator/scan_cycle.py` (lines 47-74)
- **What's wrong:** If Kalshi API is unreachable during scan, returns empty market list. No fallback to cached/stale market data.
- **Fix:** Fall back to last-known market list from database (with staleness warning) rather than trading nothing.

---

### LOW -- OPTIONAL

**L-1: Extremization Factor Hardcoded**
- **File:** `src/analysis/ensemble.py:31`
- `DEFAULT_EXTREMIZE_FACTOR = 1.15` should be configurable via settings.yaml.

**L-2: PM2 Log Rotation Requires Manual Setup**
- **File:** `ecosystem.config.js`
- Must run `pm2 install pm2-logrotate` post-deployment to prevent disk exhaustion.

**L-3: DuckDuckGo Library Has No Stable API Contract**
- **File:** `src/analysis/news_search.py`
- `ddgs` library could break on updates. Serper is more reliable for production.

**L-4: Dashboard Process Not Separate in PM2**
- **File:** `ecosystem.config.js`
- Dashboard runs in main process. Consider separate PM2 process for resilience.

**L-5: leaderboard.py Has Zero Test Coverage**
- **File:** `scripts/leaderboard.py`
- Add tests when modifying.

**L-6: Bare `except Exception` Clauses (9 instances)**
- **Files:** order_router.py, position_manager.py, ai_probability.py, fill_tracker.py (2), database.py, websocket_client.py, kalshi_client.py, news_fetcher.py
- All are properly logged/handled but should catch more specific exceptions.

**L-7: ~20 Files Exceed 300 Lines; ~40 Functions Exceed 50 Lines**
- Key offenders: `lifecycle.py` (~850 lines), `position_manager.py` (~900 lines), `scan_cycle.py` (~695 lines), `database.py` (~750 lines)
- Refactor when touching these files.

**L-8: ~100 Magic Numbers Across Codebase**
- Most are properly named constants, but some inline values in position_manager.py and order_builder.py should be extracted.

**L-9: Adversarial Adjustments Not Logged Separately**
- **File:** `src/analysis/adversarial_analyzer.py:140-182`
- Adjustments applied but not stored for post-hoc analysis.

**L-10: Exit Reason Logging Silently Fails**
- **File:** `src/orchestrator/trade_cycle.py:133-134`
- Exit reason DB write wrapped in try/except with DEBUG-only logging. Non-critical but lossy.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| **Kalshi REST** | RSA-PSS signing (excellent) | 429/401/5xx/timeout all handled | 3 retries + exponential backoff | Token bucket (8/s, burst 10) | 30s per request | Yes | EXCELLENT |
| **Kalshi WebSocket** | RSA-PSS signing | Auto-reconnect, stale detection | 10 max consecutive failures | N/A (server-push) | 30s ping/pong | Yes | EXCELLENT |
| **Anthropic (Claude)** | API key via env var | Circuit breaker after 3 failures | 3 retries + backoff for rate limit | Daily token budget (soft+hard) | 60s (SDK default) | Yes | GOOD |
| **OpenAI (GPT-4o)** | API key via env var | Returns None on failure (silent) | Retries for rate limit + connection | Separate daily budget (uncoordinated) | SDK default | Yes | NEEDS WORK |
| **Serper (Search)** | API key header | 3-strike permanent disable | No retry on 429 | None (relies on API limits) | Not configured | Yes | FAIR |
| **FRED** | API key query param | Graceful degradation if key missing | Exponential backoff | None | Not visible | Yes | GOOD |
| **Metaculus** | Bearer token | Returns empty on failure | Basic retry | None | Not visible | Yes | FAIR |
| **Manifold** | None (public API) | Returns empty on failure | Basic retry | None | Not visible | Yes | FAIR |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| **Market Discovery** | Kalshi /events endpoint, Polymarket cross-ref | Yes | Category filtering, volume/liquidity thresholds | GOOD |
| **Forecast Generation** | Claude + GPT-4o ensemble, superforecaster decomposition, prompt A/B testing | Yes | Circuit breaker, token budgets, prompt injection defense | GOOD (GPT-4o incomplete) |
| **Edge Detection** | `claude_prob - market_price`, min_edge thresholds per strategy | Yes | Divergence gate, temporal "already priced in" check | GOOD |
| **Position Sizing** | Half-Kelly with dynamic scaling, calibration-based multiplier, liquidity adjustment | Yes | 5% single position cap, confidence exponent 1.2, min 1 contract floor | EXCELLENT |
| **Order Execution** | Limit (GTC) + Market (FOK), Decimal arithmetic, cost+fee calculation | Yes | Balance pre-flight, stale price detection, price validation [1-99 cents] | EXCELLENT |
| **Position Tracking** | Fill deduplication, partial fill tracking, weighted avg entry | Yes | Transaction-wrapped DB writes, non-monotonic fill detection | GOOD |
| **P&L Calculation** | Decimal arithmetic throughout, fees included | Yes | Realized + unrealized tracking | GOOD (no strategy attribution) |
| **Settlement Handling** | WebSocket lifecycle messages, synthetic SELL trade, audit trail | Yes | Range validation [0-1], binary outcome check | GOOD |
| **Calibration** | Brier scores with time-decay, per-category, James-Stein shrinkage | Yes | Platt calibrator (50+ samples), calibration curves | EXCELLENT |
| **Circuit Breaker** | Daily loss 8%, 3 consecutive losing days = quarter-Kelly, 5 = halt | Yes | Max drawdown 20%, auto-recovery after 48h | EXCELLENT |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Overall |
|---|---|---|---|---|---|
| **src/core/kalshi_client.py** | 4/5 | 4/5 | 5/5 | 5/5 | 4.5/5 |
| **src/core/websocket_client.py** | 4/5 | 4/5 | 4/5 (race condition) | 4/5 | 4/5 |
| **src/core/models.py** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| **src/core/key_loader.py** | 5/5 | 4/5 | 5/5 | 5/5 | 4.75/5 |
| **src/core/market_discovery.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/analysis/claude_forecaster.py** | 4/5 | 4/5 | 5/5 | 5/5 | 4.5/5 |
| **src/analysis/openai_forecaster.py** | 2/5 | 3/5 | 3/5 | 2/5 | 2.5/5 |
| **src/analysis/ensemble.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/analysis/prompt_templates.py** | 5/5 | 4/5 | 5/5 | 5/5 | 4.75/5 |
| **src/analysis/forecast_parser.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/analysis/decomposer.py** | 4/5 | 4/5 | 4/5 | 3/5 | 3.75/5 |
| **src/analysis/calibration.py** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| **src/analysis/news_researcher.py** | 4/5 | 4/5 | 5/5 | 4/5 | 4.25/5 |
| **src/analysis/news_search.py** | 4/5 | 4/5 | 4/5 | 3/5 | 3.75/5 |
| **src/analysis/news_fetcher.py** | 4/5 | 4/5 | 4/5 | 3/5 | 3.75/5 |
| **src/data/market_scanner.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/data/data_enricher.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/data/consensus_aggregator.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/data/cache.py** | 4/5 | 4/5 | 4/5 | 3/5 | 3.75/5 |
| **src/execution/order_builder.py** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| **src/execution/order_router.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/execution/router_kalshi.py** | 5/5 | 4/5 | 5/5 | 5/5 | 4.75/5 |
| **src/execution/position_manager.py** | 3/5 (too long) | 4/5 | 4/5 | 4/5 | 3.75/5 |
| **src/execution/fill_tracker.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/risk/risk_engine.py** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| **src/risk/kelly_sizer.py** | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 |
| **src/risk/circuit_breaker.py** | 4/5 | 4/5 | 5/5 | 5/5 | 4.5/5 |
| **src/risk/manipulation_detector.py** | 4/5 | 4/5 | 4/5 | 3/5 | 3.75/5 |
| **src/storage/database.py** | 3/5 (too long) | 4/5 | 3/5 | 3/5 | 3.25/5 |
| **src/strategies/ai_probability.py** | 3/5 (too long) | 4/5 | 4/5 | 4/5 | 3.75/5 |
| **src/strategies/cross_arb.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/strategies/news_reactive.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/strategies/obvious_no.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |
| **src/orchestrator/lifecycle.py** | 3/5 (too long) | 4/5 | 4/5 | 4/5 | 3.75/5 |
| **src/orchestrator/scan_cycle.py** | 3/5 (too long) | 4/5 | 5/5 | 4/5 | 4/5 |
| **src/dashboard/server.py** | 4/5 | 3/5 | 4/5 | 4/5 | 3.75/5 |
| **src/alerts/alert_manager.py** | 4/5 | 4/5 | 4/5 | 4/5 | 4/5 |

**Overall Codebase Health: 4.1/5.0 (Very Good)**

---

## Improvement Roadmap Status

| Item | Status | Evidence |
|------|--------|---------|
| Kalshi market price fed into Claude prompt | IMPLEMENTED | `market.yes_price` injected into all 6 category templates (prompt_templates.py:74,98,122,...) |
| GPT-4o as second forecaster for ensemble | IMPLEMENTED (incomplete) | `OpenAIForecaster` exists with Brier-weighted ensemble, but missing budget coordination and variant support |
| Superforecaster-style prompt decomposition | IMPLEMENTED | `QuestionDecomposer` with 14 compound indicators, parallel sub-question assessment, AND/OR/CONDITIONAL recombination |
| Full article text from Serper results | IMPLEMENTED | `fetch_article_text()` with HTML parsing, JSON-LD support, sentence-boundary truncation, 50-word minimum |
| Multi-model ensemble with disagreement | IMPLEMENTED | `multi_model_ensemble()` with Brier-weighted averaging, >15pp disagreement detection, 50% sizing reduction, 20% min weight floor |
| Calibration tracking with Brier scores | IMPLEMENTED | `CalibrationTracker` with time-decay weighting, per-category James-Stein shrinkage, 10-bin calibration curves |
| Performance dashboard | IMPLEMENTED | FastAPI on port 8080 with API key auth, CORS, HTML + JSON + HTMX routes |

---

## Regulatory Compliance

| Check | Status | Notes |
|-------|--------|-------|
| Trades only on Kalshi (CFTC-regulated) | PASS | All execution via Kalshi CLOB. Polymarket is read-only cross-reference only. |
| No Polymarket execution capability | PASS | `polymarket_discovery.py` has regulatory notice; `enabled: false` by default. No order submission code for Polymarket. |
| Position limits compliance | PASS | 15-point risk check: 5% single, 40% total, 20% correlated, 8% daily loss, 20% max drawdown |
| Market manipulation safeguards | PASS | Rapid move detection, crossed book detection, volume-adjusted thresholds, 30-minute flag window |
| Tax record-keeping | PASS | `export_tax_report.py`: FIFO matching, settlement tracking, holding period classification, Form 8949 CSV export |
| CLAUDE.md documentation accuracy | FAIL | States strategies run on "Polymarket" but execution is Kalshi-only. Needs update. |

---

## Backtest & Calibration Infrastructure

| Component | Status | Quality |
|-----------|--------|---------|
| Live API backtester | Implemented | Explicitly acknowledges lookahead bias. `cached_only=True` default. |
| Offline replay engine | Implemented | Per-strategy degradation multipliers (ai_prob=0.65, news=0.50). No API calls. |
| Walk-forward optimization | Partial | Grid search over 288 parameter combinations, but not true expanding-window walk-forward. |
| Survivorship bias mitigation | Implemented | Loads ALL markets (settled + active + abandoned). Unresolved positions marked-to-worst-case. |
| Slippage & fee accounting | Implemented | Flat bps model (default 10bps) + order-book-depth-aware option. Kalshi taker fees deducted. |
| Brier score tracking | Implemented | Exponential time-decay (30-day half-life), per-category, bucketed by staleness. |
| Calibration curves | Implemented | 10-bin predicted vs actual. Category-specific James-Stein shrinkage. |
| Selection bias analysis | Partial | Filters to volume >= 100 contracts. Could underestimate slippage on low-liquidity markets. |

---

## Error Handling & Graceful Degradation

| Scenario | Detection | Recovery | Rating |
|----------|-----------|----------|--------|
| Anthropic API down | Startup health check (single attempt) | Degraded mode: AI strategies disabled, whale+obvious-NO continue | FAIR (no retry at startup) |
| Kalshi API down | Circuit breaker (5 consecutive 5xx) | Block 60-600s, half-open recovery requires 3 successes | EXCELLENT |
| Serper/News down | Caught in strategy scan | News-reactive strategy skipped, others continue | GOOD |
| Internet drops mid-trade | Connection timeout + pool reset | 3 retries, post-timeout position sync | GOOD |
| PM2 restart | PID lock + state persistence | Positions from DB, pending orders from DB, bankroll re-synced from Kalshi | GOOD |
| Position desync | Periodic sync, 3 failure threshold | Circuit breaker halt after 3 consecutive sync failures | EXCELLENT |
| Database failure | Disk space check at startup | 5% critical / 10% warning thresholds. No DB connection retry. | FAIR |
| Circuit breaker halt | Multiple triggers (loss, drawdown, desync) | Blocks new entries, allows stop-loss exits, continues monitoring | EXCELLENT |

---

## Security Summary

| Check | Status | Notes |
|-------|--------|-------|
| Secrets in source code | PASS | No API keys in .py files (only test fixtures with `test-key`) |
| Secrets in config/.env | FAIL (C-1) | Real keys on disk. Rotate immediately. |
| .gitignore coverage | PASS | .env, .pem, credentials all excluded |
| Command injection | PASS | Only 1 subprocess call, uses list-based command (no `shell=True`) |
| HTTPS for all APIs | PASS | All external calls use httpx with TLS |
| Sensitive data in logs | PASS | API keys redacted, no PII logging |
| File permissions | PASS | Startup auto-fixes loose permissions to 0600 |
| Prompt injection defense | PASS | 4-layer defense: truncation, control char stripping, pattern matching, character allowlist |

---

## Top 10 Recommendations (Prioritized)

### 1. Rotate All Exposed API Keys (IMMEDIATE)
**Risk:** Financial loss from unauthorized API usage or trading.
Rotate ANTHROPIC_API_KEY, KALSHI_API_KEY_ID, POLYMARKET_PRIVATE_KEY, SERPER_API_KEY, METACULUS_API_TOKEN. Verify git history is clean.

### 2. Resolve GPT-4o Integration Status (HIGH)
**Risk:** Silent ensemble degradation, uncoordinated costs.
Either complete with budget coordination + variant support, or disable in config until ready. Half-integrated is worse than disabled.

### 3. Add Explicit Buying Power Tracking (HIGH)
**Risk:** Over-commitment of capital on concurrent orders.
Compute `remaining_balance = balance - sum(pending_costs)` before every order submission.

### 4. Fix WebSocket Subscription Race Condition (HIGH)
**Risk:** Missed fill notifications or price updates during reconnection.
Wrap subscription mutations and callback iterations in async lock.

### 5. Add Per-Strategy Position Limits and P&L Attribution (HIGH)
**Risk:** Over-concentration in degraded strategy.
Cap per-strategy exposure (e.g., 15%). Track per-strategy realized P&L. Auto-reduce weight when strategy Brier degrades.

### 6. Implement Partial Fill Policy (HIGH)
**Risk:** Consistently undersized positions, missed trades.
Define explicit retry/cancel/accept behavior for partial fills. Recalculate Kelly on significant price deviation.

### 7. Add Anthropic Startup Retry Logic (MEDIUM)
**Risk:** Entire AI pipeline disabled by transient outage at startup.
3 attempts with exponential backoff before entering degraded mode.

### 8. Extend Wash-Trade Cooldown to Event Level (MEDIUM)
**Risk:** Inadvertent wash-trade patterns across correlated markets.
Apply cooldown to `event_ticker`, not just `market_id`.

### 9. Update CLAUDE.md Documentation (MEDIUM)
**Risk:** Confusion about which platform the bot trades on.
Change "Polymarket" references to "Kalshi" throughout. Clarify Polymarket is read-only cross-reference.

### 10. Implement True Walk-Forward Backtesting (MEDIUM)
**Risk:** Overfitting to historical data without out-of-sample validation.
Add expanding-window walk-forward optimization with re-estimation at checkpoints.

---

## Conclusion

**Overall Assessment: 4.1/5.0 -- Very Good, Production-Ready with Caveats**

The PolyEdge codebase demonstrates strong engineering across its core trading infrastructure:
- **Excellent:** Kalshi API integration, Decimal monetary arithmetic, risk engine (15 checks), Kelly sizing, circuit breaker, calibration tracking, prompt injection defense
- **Good:** Claude forecasting pipeline, error handling, retry logic, state persistence, tax compliance
- **Needs Work:** GPT-4o integration, partial fill handling, per-strategy attribution, WebSocket race condition

**The single most urgent action is rotating the exposed API keys in config/.env.** After that, resolve the GPT-4o integration status (complete or disable) before the next major trading cycle.

The system is architecturally sound with clean module boundaries, no circular dependencies, comprehensive test coverage (97+ test files), and 100% pinned dependencies. The 15-point risk engine, circuit breaker, and Decimal arithmetic provide a solid safety foundation for live trading.
