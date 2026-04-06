# PolyEdge — Complete Codebase Audit

Perform a comprehensive, line-by-line audit of this entire codebase. This is an AI-driven prediction market trading bot targeting Kalshi (CFTC-regulated), built with Python 3.12+, running on a Mac Mini M4 Pro via pm2. It uses Claude Sonnet/Opus via Anthropic API and GPT-4o via OpenAI API for multi-model ensemble forecasting, with 8 trading strategies running simultaneously. Every file, every function, every line must be examined. This system trades real money — missed bugs mean lost capital.

**Codebase stats:** ~68K LOC across 120 Python source files, 97+ test files across 12 test directories, 8 external API integrations, 8 trading strategies, 18 pinned dependencies, 12+ environment variables, SQLite WAL mode database, FastAPI dashboard.

---

## 1. Structural Integrity

- Map the complete directory tree (all files, all directories) — include `src/core/`, `src/analysis/`, `src/data/`, `src/execution/`, `src/risk/`, `src/storage/`, `src/strategies/`, `src/alerts/`, `src/dashboard/`, `src/orchestrator/`, `scripts/`, `tests/`, `config/`, `data/`
- Count all source files by directory (expect ~120 across 11 packages)
- Count all test files/suites by directory (expect ~97 across 12 test directories)
- List every module and its purpose — cross-reference against CLAUDE.md project structure
- Identify any orphaned files (not imported by anything)
- Identify any dead code (exported but never imported elsewhere)
- Check that CLAUDE.md accurately reflects the current codebase state — flag any discrepancies (new files, removed files, renamed modules, changed APIs)
- Verify all config files are present and valid (`pyproject.toml`, `requirements.txt`, `Makefile`, `config/settings.yaml`, `config/.env.example`, pm2 ecosystem config)
- Check that ALL 18 Python dependencies are pinned to exact versions (no `>=`, `~=`, or `*`)
- List all dependencies and flag any that are unused, outdated, or have known CVEs
- Run `pip check` for dependency conflicts
- Verify the pm2 ecosystem config is correct and covers all processes

## 2. Configuration & Environment

- Grep every file for `os.environ`, `os.getenv`, `dotenv`, `settings.` references — compile the COMPLETE list of all 12+ env vars used in code
- Cross-reference against `config/.env.example` and CLAUDE.md documentation — flag any undocumented env vars
- Check for hardcoded values that should be env vars (API keys, endpoints, secrets, thresholds, timeouts)
- Verify all API keys and secrets are loaded from env, never hardcoded:
  - `ANTHROPIC_API_KEY` (Claude API)
  - `OPENAI_API_KEY` (GPT-4o)
  - `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`
  - `SERPER_API_KEY` (news/search data)
  - `FRED_API_KEY` (Federal Reserve economic data)
  - `METACULUS_API_TOKEN` (cross-platform reference)
  - `POLYMARKET_PRIVATE_KEY` (if still present — should be removed or clearly marked as unused)
- Check `config/settings.yaml` for all configurable parameters — verify defaults are sensible
- Verify all API endpoint URLs are configurable (not hardcoded to production)
- Check for any test/sandbox vs production environment switching logic
- Verify `POLYEDGE_LIVE_ENABLED` env var gate is enforced everywhere it should be

## 3. Credential Security & Key Management

- **CRITICAL CHECK**: Scan ALL files (including config/) for actual API keys, private keys, or secrets on disk
  - Grep for patterns: `sk-ant`, `sk-proj`, `api_key`, `private_key`, `secret`, `token`, `password`, `bearer`
  - Check `config/.env` — are real production keys present?
  - Check `git log --all --full-history -- config/.env` — was .env ever committed?
- Verify `.gitignore` covers ALL sensitive files (`.env`, `*.key`, `data/*.db`, logs)
- Verify `src/core/key_loader.py`:
  - Private key loading from file path (not hardcoded)
  - Proper file permissions check on key files
  - Graceful handling of missing/corrupt key files
- Check for any credentials in log output, error messages, or API responses
- Evaluate whether a pre-commit hook exists to block `.env` commits
- Check for sensitive data in `data/` directory (SQLite databases, cache files, logs)

## 4. Integration Health — Kalshi REST API

- Trace EVERY Kalshi API call in `src/core/kalshi_client.py` — list all endpoints used
- Audit authentication handling:
  - API key + private key signature flow
  - Token refresh/expiry handling
  - Session management
  - Graceful handling of auth failures (401, 403)
- Verify rate limiting compliance (Kalshi has strict rate limits):
  - Rate limit headers parsed and respected
  - Exponential backoff with jitter on 429 responses
  - Request queuing or throttling mechanism
- Check error handling for EVERY Kalshi API call (timeouts, 500s, 429s, malformed data, network errors)
- Verify order placement logic:
  - Correct price formatting (cents, proper rounding)
  - Proper side handling (yes/no)
  - Limit order vs market order logic
  - Order confirmation/verification after placement
  - Maker-preferred order routing
- Check position tracking accuracy (sync with on-chain/API positions)
- Verify balance/buying power checks before placing orders
- Check settlement handling (expired/settled markets)
- Verify market discovery and filtering logic (volume, liquidity, category thresholds)
- Check for proper handling of market status changes (open, closed, halted, settled)
- Verify all monetary calculations use `Decimal` or integer cents (NO floating point for money)
- Check for proper pagination handling on list endpoints

## 5. Integration Health — Kalshi WebSocket

- Audit `src/core/websocket_client.py`:
  - Connection lifecycle (connect, subscribe, reconnect, disconnect)
  - Heartbeat/ping-pong mechanism
  - Auto-reconnect with exponential backoff
  - Message ordering and deduplication
- **CRITICAL CHECK**: Thread safety of subscription management
  - `subscribe()` and `unsubscribe()` — is `_subscriptions` set protected by async lock?
  - `_price_callbacks` and `_fill_callbacks` — protected during iteration in `_dispatch_message()`?
  - Race conditions during reconnection (subscriptions re-established atomically?)
- Verify price update feed accuracy (stale data detection)
- Verify fill notification handling (order completion, partial fills)
- Check error handling for WebSocket failures (connection drops, malformed messages)
- Verify subscription management (add/remove markets, cleanup on position exit)

## 6. Integration Health — Anthropic API (Claude)

- Audit `src/analysis/claude_forecaster.py`:
  - Model selection: sonnet for routine, opus for high-stakes (>$50 positions)
  - Prompt construction quality (system prompt, user prompt, category-specific templates)
  - Response JSON parsing robustness (what happens if Claude returns unexpected format?)
  - Token usage tracking and daily budget enforcement
  - Timeout handling, retry logic with exponential backoff
  - Circuit breaker integration
- Check `src/analysis/prompt_templates.py`:
  - Templates per category: POLITICS, FED_MACRO, GEOPOLITICS, TECH_AI, CULTURE, GENERAL
  - Are resolution criteria included verbatim?
  - Is market price fed into prompt for calibration?
  - Are base rates requested?
  - Is confidence interval requested?
  - Superforecaster-style decomposition techniques used?
- Verify prompt A/B testing infrastructure (if enabled)
- Check token cost tracking per model (sonnet vs opus)
- Verify all Claude API calls have proper error handling, timeout, and retry

## 7. Integration Health — OpenAI API (GPT-4o)

- Audit `src/analysis/openai_forecaster.py`:
  - Model initialization and API key loading
  - Prompt construction (does it match Claude's quality?)
  - Response parsing robustness
  - **CRITICAL CHECK**: Feature parity with Claude forecaster:
    - Token budget coordination with Claude's daily budget system
    - Circuit breaker tie-in
    - Prompt A/B testing support (check if hardcoded `enabled=False`)
    - Category-specific model selection
    - `accuracy_context` threading through prompts
  - What happens when GPT-4o returns `None` or errors? (silent skip vs proper error signaling)
- Verify `src/analysis/ensemble.py`:
  - How are Claude + GPT-4o forecasts combined? (weighted average, extremization?)
  - Disagreement handling when models diverge significantly
  - Graceful degradation when one model is unavailable
  - Is ensemble weight based on Brier score performance?
  - Does ensemble silently degrade to single-model when GPT-4o fails?
- Check combined daily API cost tracking (both Anthropic + OpenAI)

## 8. Integration Health — Data Sources

- Audit Serper API integration (`src/data/news_ingestion.py`):
  - Search query construction quality
  - Full article text fetching (not just snippets)
  - Data freshness handling
  - URL deduplication
  - Error handling, timeout, retry logic
  - Caching strategy for repeated searches
- Audit FRED API integration (`src/data/` modules):
  - Federal Reserve economic data fetching
  - FedWatch probability parsing
  - Error handling for API failures
- Audit Metaculus API integration:
  - Cross-platform reference data
  - API token authentication
  - Error handling
- Check for any web scraping components and their robustness
- Verify data preprocessing before feeding to LLM
- Check for stale data detection across all data sources

## 9. Trading Strategies

- Audit ALL 8 strategies in `src/strategies/`:
  - `ai_probability.py` — Claude/GPT-4o probability → edge calculation → signal generation. Min edge threshold check.
  - `cross_arb.py` — Intra-market (yes+no<0.98), logical/subset, mutual exclusivity. Claude validation of relationships.
  - `cross_platform_arb.py` — Kalshi vs Polymarket discrepancies (min similarity 0.55). Verify Polymarket execution code is removed (cross-reference only).
  - `whale_tracker.py` — Proven wallet monitoring, 80%+ basket consensus signal. Wallet dedup, timing weight.
  - `news_reactive.py` — Breaking news → Claude impact assessment → trade before repricing. Target <30s news-to-signal.
  - `late_resolution.py` — Markets resolving <6h, outcome >90% certain but priced <80%.
  - `obvious_no.py` — Absurd markets (YES at $0.01-$0.05), buys NO. Max 10% bankroll cap. Annualized return >20% filter.
  - `mean_reversion.py` — Fades >10% moves in 2h, max 2% bankroll, auto-closes in 4h.
- For EACH strategy verify:
  - Edge threshold is enforced
  - Risk checks are called before signal generation
  - Signals include proper reasoning and confidence
  - Strategy-specific position limits exist
  - Per-strategy P&L tracking is accurate
  - Cooldown between trades to prevent wash trading
- Check for strategy interactions (can two strategies take opposing positions on the same market?)
- Verify strategy orchestration in `src/orchestrator/` — priority, scheduling, conflict resolution

## 10. Execution & Order Management

- Audit `src/execution/order_builder.py`:
  - `build_limit_order()` and `build_market_order()` — validation, price sanity, fee rate
  - Balance + risk limit validation before order construction
  - Maker-preferred GTC defaults
- Audit `src/execution/order_router.py`:
  - Paper mode vs live mode routing
  - **CRITICAL CHECK**: Buying power race condition — is `remaining_balance = balance - pending_cost` computed atomically?
  - Order submission → fill monitoring → position update flow
  - Partial fill handling: retry remainder? Cancel? Accept? Log?
  - Stale order management (cancel-and-replace)
  - Three-gate safety system enforcement:
    1. `trading.mode: "live"` in config
    2. `POLYEDGE_LIVE_ENABLED=true` env var
    3. Manual confirmation on first live trade per session
- Audit `src/execution/position_manager.py`:
  - Position sync with Kalshi API (reconcile paper vs actual)
  - Unrealized P&L calculation accuracy
  - Exit condition evaluation (`should_exit()`)
  - Correlated exposure tracking via market graph
- Audit `src/execution/fill_tracker.py`:
  - Fill event processing from WebSocket
  - Slippage tracking
  - Post-fill Kelly recalculation (check if implemented)

## 11. Risk Management

- Audit `src/risk/risk_engine.py` — ALL 15 checks must pass:
  1. `check_balance` — sufficient funds
  2. `check_position_limit` — 5% max per position
  3. `check_total_exposure` — 40% max total
  4. `check_correlated_exposure` — 20% max correlated
  5. `check_daily_loss_limit` — 10% daily max
  6. `check_market_liquidity` — <2% slippage
  7. `check_existing_position` — no double-entry
  8. `check_edge_minimum` — 5% AI, 2% arb
  9. `check_resolution_date` — market still active
  10. `check_cooldown` — time between trades
  11-15. List remaining checks
  - Are ALL checks enforced on every order? Any bypass paths?
  - Per-strategy risk limits (not just global)
- Audit `src/risk/kelly_sizer.py`:
  - Half-Kelly calculation: `f = (p*b - q) / b` at 50%
  - Position cap at min(f×bankroll, bankroll×5%)
  - Edge-proportional sizing
  - Reduction near exposure limit
- Audit `src/risk/circuit_breaker.py`:
  - Daily loss > 10% → halt all trading
  - 3 consecutive losing days → quarter-Kelly
  - 5 consecutive → halt + alert
  - Manual override mechanism
- Audit `src/risk/portfolio_risk.py`:
  - Correlated exposure calculation
  - Category diversification tracking
- Check `src/risk/manipulation_detection.py` (if exists):
  - Wash trading prevention (cooldown between opposing trades on same market)
  - Market manipulation safeguards

## 12. Data Storage & Persistence

- Audit `src/storage/database.py`:
  - SQLite WAL mode configuration
  - Version-based migration system
  - Connection pooling and concurrent access handling
- Audit storage mixin modules (`src/storage/`):
  - Markets, trades, calibration, risk, stats, whales
  - **CHECK**: Are prices stored as float or integer cents? (float in DB = precision loss risk)
  - Proper indexing for common queries
- Check data retention and pruning policies
- Verify state persistence across restarts (open positions, pending orders, circuit breaker state)
- Check for database locking issues under concurrent read/write
- Verify backup strategy for `data/markets.db`

## 13. Calibration & Performance Tracking

- Audit `src/analysis/calibration.py`:
  - `log_prediction()`, `resolve_prediction()`
  - Brier score calculation (0.0 perfect, 0.25 random) — target <0.20
  - Calibration curve generation (binned predicted vs actual)
  - Performance by market category
  - Performance by probability bucket (extreme vs uncertain)
  - Edge vs actual outcome tracking
- Audit `src/analysis/market_classifier.py`:
  - Category classification accuracy (keyword + Gamma tags)
  - Binary/clear resolution/days to resolution/liquidity tier classification
- Check for selection bias in performance reporting
- Verify per-strategy performance attribution is accurate (not just aggregate)
- Check historical data sourcing for backtests

## 14. Alerts & Monitoring

- Audit `src/alerts/alert_manager.py`:
  - Alert dispatch routing
  - Alert priority levels
  - Cooldown/dedup to prevent alert storms
- Audit `src/alerts/imessage_alert.py`:
  - iMessage integration (bridge to VAYU infra via HTTP)
  - Alert types: signal detected, trade executed, daily P&L, circuit breaker
  - One-click trade URLs (if implemented)
- Audit `src/alerts/daily_report.py`:
  - EOD summary: trades, P&L, win rate, biggest win/loss, 7-day Brier, strategy comparison
  - Delivery timing and reliability
- Audit `src/dashboard/server.py`:
  - FastAPI web UI routes
  - Portfolio overview, strategy breakdown, calibration chart
  - Authentication on dashboard endpoints
  - WebSocket feed for real-time updates (if implemented)

## 15. Error Handling & Reliability

- Check EVERY try/except block — are errors properly logged? Silently swallowed? Re-raised?
- Verify retry logic for ALL external API calls:
  - Kalshi REST (with backoff)
  - Kalshi WebSocket (reconnect)
  - Anthropic Claude (with budget awareness)
  - OpenAI GPT-4o (with proper failure signaling)
  - Serper, FRED, Metaculus
- Check for proper timeout configuration on ALL HTTP requests
- Verify graceful degradation:
  - What happens if Anthropic API is down? (ensemble degrades to GPT-4o only)
  - What happens if OpenAI API is down? (ensemble degrades to Claude only)
  - What happens if Kalshi API is down? (halt trading, preserve state)
  - What happens if Kalshi WebSocket drops mid-trade?
  - What happens if all data sources are down? (halt, alert, resume when recovered)
- Check for race conditions in async operations (especially order routing, WebSocket dispatch)
- Verify pm2 restart behavior — does the bot recover cleanly from crashes?
- Check for proper state persistence — if the bot restarts, does it know its open positions?
- Verify logging captures enough detail to diagnose production issues
- Check for memory leaks (growing lists, unclosed connections, accumulated data in long-running processes)
- Audit startup sequence in `src/orchestrator/startup.py`:
  - Retry logic when APIs are unavailable at boot (Anthropic, Kalshi)
  - Order of initialization (dependencies satisfied before use)

## 16. Code Quality

- Find functions over 50 lines (Python functions should be concise)
- Find files over 300 lines (candidates for splitting)
- Check for TODO/FIXME/HACK/XXX comments — list them ALL with context
- Check for bare `except:` clauses (should catch specific exceptions)
- Check for mutable default arguments
- Verify type hints on ALL function signatures (Pydantic models throughout)
- Check for f-string vs `.format()` consistency
- Look for magic numbers that should be named constants
- Check for proper docstrings on all public functions
- Verify logging uses proper log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Check for any `print()` statements that should be logging
- Look for copy-pasted code blocks that should be shared utilities
- Check imports are organized (stdlib, third-party, local)
- Run `mypy` and report all type errors
- Run `ruff` or `flake8` and report all lint issues

## 17. Testing & Quality Assurance

- Run `pytest` and report total passing/failing/skipped across all 97+ test files
- Run `pytest --cov=src --cov-report=html` and report coverage by module
- Identify any tests that are skipped (`.skip`) or marked as TODO
- Check for tests with no assertions (empty test bodies)
- Check for tests that only test happy paths (no error/edge case coverage)
- Identify test files where all tests use the same mock (no real integration)
- Report the 5 largest test files by line count
- Report the 5 smallest test files by test count (potentially undertested)
- Check integration test coverage (tests that hit real APIs with mocks)
- Verify paper trading has proper test coverage
- Assess ratio of unit tests vs integration tests vs E2E tests
- Check that test data doesn't contain real API keys or PII

## 18. Regulatory Compliance

- Verify the bot only actively trades on Kalshi (CFTC-regulated, legal for US users)
- **CHECK**: Is Polymarket execution code fully removed? (cross-platform arb should reference Polymarket prices but NOT execute trades there — not legal for US residents)
  - Scan for any remaining Polymarket trade execution calls
  - Verify cross_platform_arb.py only reads Polymarket prices, never submits orders
  - Check if `POLYMARKET_PRIVATE_KEY` env var is still needed (if only reading public data, no)
- Verify there's no attempt to circumvent Kalshi's terms of service
- Check position limits compliance
- Verify the bot doesn't attempt to manipulate markets (wash trading, spoofing)
- Check for proper record-keeping of all trades (required for tax reporting)
- Verify `scripts/tax_export.py` produces accurate records

## 19. Backtesting & Historical Analysis

- Audit `scripts/run_backtest.py`:
  - Historical data sourcing and accuracy
  - Backtest methodology (walk-forward, out-of-sample)
  - Are backtest results realistic (accounting for slippage, fees, market impact)?
  - Selection bias prevention
- Audit `scripts/parameter_replay.py`:
  - Parameter sensitivity analysis
  - Overfitting detection
- Audit `scripts/backfill_markets.py`:
  - Historical market data backfill
  - Data integrity checks
- Verify logging of all trading decisions:
  - Forecast vs actual outcome
  - Entry price, exit price, P&L per trade
  - Model used, prompt version, data sources
  - Timestamp accuracy and timezone consistency

---

## 20. Upgrade & Improvement Opportunities — Architecture

Analyze the codebase for architectural upgrades that would improve reliability, scalability, and returns:

- **Ensemble maturity**: GPT-4o integration needs feature parity with Claude — evaluate the gap and propose a completion plan (budget coordination, circuit breaker, A/B testing, category selection)
- **Model expansion**: Evaluate adding Gemini 2.5 Pro or other models to the ensemble — what would the integration pattern look like?
- **Event-driven execution**: Currently poll-based scanning — evaluate whether WebSocket-driven event processing would reduce latency for news-reactive and price-change strategies
- **Strategy isolation**: Evaluate whether each strategy should run in its own process/thread for fault isolation (one strategy crash doesn't halt others)
- **Database scaling**: SQLite WAL is sufficient for now — evaluate the upgrade path to Postgres for concurrent access, better analytics, and remote dashboard
- **Caching layer**: Evaluate whether Redis/memcached for market data, forecasts, and signals would reduce API calls and improve latency
- **Configuration hot-reload**: Evaluate whether settings.yaml changes could be applied without restart (strategy thresholds, risk limits, model selection)

## 21. Upgrade & Improvement Opportunities — Trading & Risk

Analyze trading logic for improvements that could increase returns and reduce risk:

- **Position sizing refinement**: Half-Kelly is conservative — evaluate adaptive Kelly based on recent Brier scores (increase when calibrated, decrease when degraded)
- **Partial fill management**: Implement explicit partial fill policy (retry remainder, cancel, accept) with post-fill Kelly recalculation
- **Buying power reservation**: Implement atomic balance reservation to prevent concurrent order race conditions
- **Per-strategy limits**: Currently global risk limits only — evaluate per-strategy position and loss limits to prevent one strategy from consuming all capital
- **Drawdown-based scaling**: Evaluate scaling position sizes down proportionally during drawdown periods (beyond circuit breaker halt)
- **Market microstructure**: Evaluate order book depth analysis before order placement (not just top-of-book price)
- **Slippage model**: Build a slippage prediction model from historical fill data to improve Kelly sizing accuracy
- **Exit optimization**: Evaluate whether dynamic exit triggers (trailing stop, time-based decay, probability re-evaluation) would improve realized returns vs current static exit conditions

## 22. Upgrade & Improvement Opportunities — AI & Forecasting

Analyze the AI pipeline for accuracy and efficiency improvements:

- **Prompt optimization**: Review all category-specific prompt templates — are they using the latest research on superforecasting decomposition? Can they be improved with chain-of-thought or structured output modes?
- **Calibration feedback loop**: Evaluate whether feeding recent calibration performance back into prompts ("you've been 3% overconfident on politics markets this week") would improve accuracy
- **News quality scoring**: Not all news sources are equally reliable — evaluate source credibility weighting in the news ingestion pipeline
- **Market context enrichment**: Evaluate whether enriching prompts with related market prices, historical resolution patterns, and expert commentary would improve forecast quality
- **Cost optimization**: Evaluate model routing — can most markets use Sonnet while reserving Opus only for complex/high-stakes? What's the accuracy vs cost tradeoff?
- **Ensemble weighting**: Evaluate dynamic ensemble weights based on per-category Brier scores (weight models higher in categories where they perform better)
- **Forecast caching**: Evaluate whether caching forecasts for slow-moving markets (re-forecast only when significant news or price change occurs) would reduce costs without sacrificing accuracy

## 23. Upgrade & Improvement Opportunities — Operations & Reliability

Analyze operational patterns for production hardening:

- **Monitoring depth**: Evaluate adding Prometheus metrics for all key operations (orders placed, fills, API latency, forecast accuracy, P&L)
- **Alerting tiers**: Evaluate tiered alerting (info → warning → critical with escalation) beyond current iMessage alerts
- **Disaster recovery**: What happens if the Mac Mini fails? Evaluate backup strategy for database, configuration, and state
- **Log aggregation**: Evaluate centralized structured logging for easier debugging across strategies and modules
- **Health checks**: Evaluate deep health checks (all APIs reachable, database writable, sufficient balance, no stale data) beyond current status endpoint
- **Deployment automation**: Evaluate CI/CD pipeline for automated testing, linting, and deployment
- **Secret rotation**: Evaluate automated secret rotation schedule and tooling (Kalshi API keys, Anthropic key)
- **Performance profiling**: Evaluate per-strategy latency profiling to identify bottlenecks in the scan → forecast → trade pipeline

## 24. Upgrade & Improvement Opportunities — Features

Identify new capabilities that would increase edge or reduce risk:

- **Multi-market correlation**: Evaluate building a real-time correlation matrix to identify when markets move together (improve correlated exposure detection)
- **Sentiment analysis**: Evaluate social media sentiment (Twitter/X, Reddit) as an additional signal for news-reactive strategy
- **Whale wallet expansion**: Evaluate automated whale discovery (not just manual curation) based on leaderboard performance metrics
- **Resolution prediction**: Evaluate building a model that predicts WHEN markets will resolve (not just the outcome) to optimize entry timing
- **Portfolio optimization**: Evaluate Markowitz-style portfolio optimization across all active positions to maximize risk-adjusted returns
- **Tax-loss harvesting**: Evaluate automated tax-loss harvesting (exit losing positions near year-end, re-enter after wash sale period)
- **Cross-exchange data**: Evaluate consuming Metaculus and Manifold forecasts as additional ensemble inputs (free crowd wisdom)
- **Dashboard enhancements**: Evaluate adding strategy-level drill-down, trade replay, and what-if scenario analysis to the FastAPI dashboard

---

## Output Format

Produce a structured report with:

### Summary Dashboard
- Total source files, total lines of code, total test count (passing/failing/skipped)
- External API integrations (count and status)
- Trading statistics if available (total trades, win rate, P&L)
- Env var count (total, documented, undocumented)
- Test coverage estimate (by module)
- Current system health (pm2 status, API connectivity, balance)

### Issues by Severity

**🔴 CRITICAL** (could lose money, security holes, regulatory issues) — FIX BEFORE NEXT TRADE
**🟠 HIGH** (reliability issues, missing error handling, data accuracy, race conditions) — FIX THIS WEEK
**🟡 MEDIUM** (code quality, missing tests, performance, incomplete features) — FIX WHEN POSSIBLE
**🟢 LOW** (style, documentation, minor improvements) — OPTIONAL

For EVERY issue include:
- File path and line number(s)
- What's wrong (specific, not vague)
- Impact (what could go wrong — in dollars if applicable)
- Suggested fix (actionable, 1-3 sentences)

### API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Circuit Breaker | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|---|
| Kalshi REST | | | | | | | | |
| Kalshi WebSocket | | | | | | | | |
| Anthropic (Claude Sonnet/Opus) | | | | | | | | |
| OpenAI (GPT-4o) | | | | | | | | |
| Serper (News/Search) | | | | | | | | |
| FRED (Economic Data) | | | | | | | | |
| Metaculus (Cross-Platform) | | | | | | | | |
| Manifold (Cross-Platform) | | | | | | | | |

### Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | | | | |
| AI Forecast (Claude) | | | | |
| AI Forecast (GPT-4o) | | | | |
| Ensemble Logic | | | | |
| Edge Detection | | | | |
| Position Sizing (Kelly) | | | | |
| Order Execution | | | | |
| Fill Tracking | | | | |
| Position Tracking | | | | |
| P&L Calculation | | | | |
| Settlement Handling | | | | |
| Partial Fill Handling | | | | |

### Strategy Scorecard

| Strategy | Implementation | Tests | Risk Controls | Edge Threshold | Per-Strategy P&L | Status |
|---|---|---|---|---|---|---|
| AI Probability | | | | 5% | | |
| Cross-Market Arb | | | | 2% | | |
| Cross-Platform Arb | | | | 2% | | |
| Whale Tracking | | | | 80% consensus | | |
| News-Reactive | | | | min shift | | |
| Late Resolution | | | | 90%/>80% gap | | |
| Mean Reversion | | | | 10% move | | |
| Obvious NO | | | | 20% annualized | | |

### Module-by-Module Scorecard

For each module/file, rate 1-5:
- Code quality
- Test coverage
- Error handling
- Risk controls (where applicable)
- Documentation
- Overall health

### Upgrade & Improvement Roadmap (Prioritized)

Consolidate findings from sections 20-24 into a prioritized roadmap:

**Phase 1 — Quick Wins (1-2 days each)**
List improvements that are high-impact, low-effort.

**Phase 2 — Medium Term (1-2 weeks each)**
List improvements requiring moderate effort but significantly improving reliability or returns.

**Phase 3 — Strategic (1+ months)**
List architectural changes or major features that would transform the system.

For each improvement include:
- What to change (specific files, modules, or patterns)
- Why it matters (risk reduction, return improvement, cost savings)
- Estimated effort (hours/days/weeks)
- Risk level (safe refactor vs breaking change)
- Dependencies (what needs to happen first)

### Top 15 Recommendations (Prioritized)

The most impactful improvements ranked by:
1. **Risk reduction** — Could this lose money or expose credentials?
2. **Reliability** — Could this cause missed trades or phantom trades?
3. **Returns** — Could this improve forecast accuracy or execution quality?
4. **Regulatory** — Could this create compliance issues?
5. **Code quality** — Maintainability, testability, readability

### Regression Check

Verify that all findings from previous audit rounds remain resolved. Flag any regressions. Cross-reference against:
- Original 40-issue audit (all marked fixed)
- Follow-up 11-finding audit (all marked fixed)
- April 5 re-audit findings (2C, 8H, 14M, 10L)

---

**CRITICAL REMINDER**: This system trades real money on regulated markets. A bug in order placement could result in unintended positions worth hundreds or thousands of dollars. A bug in position tracking could lead to over-leveraging. A bug in the forecasting pipeline could lead to systematically bad trades. A race condition in order routing could over-commit capital. An incomplete ensemble integration could produce biased forecasts. Exposed API keys could drain the trading account. Audit with the same rigor you would apply to a financial system — because that's what this is.

**Be thorough. Read EVERY file. Check EVERY function. Verify EVERY integration. This audit should be exhaustive, not superficial.**
