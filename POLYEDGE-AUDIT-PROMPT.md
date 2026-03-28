# PolyEdge — Complete Codebase Audit

Perform a comprehensive, line-by-line audit of this entire codebase. This is an AI-driven prediction market trading bot targeting Kalshi (CFTC-regulated), built with Python 3.12, running on a Mac Mini M4 Pro via pm2. It uses Claude Sonnet via Anthropic API for AI forecasting, with plans for multi-model ensemble. Every file, every function, every line must be examined. This system trades real money — missed bugs mean lost capital.

## 1. Structural Integrity

- Map the complete directory tree (all files, all directories)
- Count all source files by directory
- Count all test files/suites
- List every module and its purpose
- Identify any orphaned files (not imported by anything)
- Identify any dead code (exported but never imported elsewhere)
- Verify all config files are present and valid (pm2, .env, requirements.txt/pyproject.toml)
- Check that all Python dependencies are pinned to specific versions
- List all dependencies and flag any that are unused, outdated, or have known CVEs
- Verify the pm2 ecosystem config is correct and covers all processes

## 2. Configuration & Environment

- Grep every file for os.environ, os.getenv, dotenv references — compile the COMPLETE list of env vars
- Cross-reference against .env.example or documentation — flag any undocumented env vars
- Check for hardcoded values that should be env vars (API keys, endpoints, secrets, thresholds)
- Verify Anthropic API key configuration and usage
- Verify Kalshi API authentication (API key, secret, or OAuth)
- Verify Serper API key configuration (for news/search data)
- Check if any API keys or secrets are committed to git
- Verify all API endpoint URLs are configurable (not hardcoded to production)
- Check for any test/sandbox vs production environment switching

## 3. Kalshi Integration

- Trace every Kalshi API call — list all endpoints used
- Check authentication handling (token refresh, expiry, session management)
- Verify rate limiting compliance (Kalshi has strict rate limits)
- Check error handling for every Kalshi API call (timeouts, 500s, rate limit responses, malformed data)
- Verify order placement logic:
  - Correct price/quantity formatting
  - Proper side (yes/no) handling
  - Limit order vs market order logic
  - Order confirmation/verification after placement
- Check position tracking accuracy
- Verify balance/buying power checks before placing orders
- Check settlement handling (how does the bot handle expired/settled markets?)
- Verify market discovery and filtering logic
- Check for proper handling of market status changes (open, closed, halted, settled)
- Verify all monetary calculations use Decimal or integer cents (NO floating point for money)
- Check websocket connections if used (heartbeat, reconnection, message ordering)

## 4. AI Forecasting Pipeline

- Audit the Claude Sonnet integration:
  - Prompt engineering quality (system prompt, user prompt construction)
  - Are prompts using superforecaster-style decomposition techniques?
  - Is the Kalshi market price fed into the prompt for calibration?
  - Temperature and model parameter settings
  - Response parsing robustness (what happens if Claude returns unexpected format?)
  - Token usage tracking and cost monitoring
  - Timeout handling for API calls
  - Retry logic with exponential backoff
- Check if GPT-4o is integrated as a second forecaster for ensemble averaging
- Verify ensemble logic if multiple models are used:
  - How are forecasts combined? (simple average, weighted, extremize?)
  - Is there disagreement handling?
- Check forecast calibration:
  - How does the system perform on extreme-probability markets (>80% or <20%)?
  - How does it handle uncertain 30-70% range markets?
  - Is there any calibration tracking or logging?
- Verify the full pipeline: data gathering → prompt construction → API call → response parsing → probability extraction → trading decision

## 5. Data Pipeline & News Integration

- Audit Serper API integration for fetching article results
- Check if full article text is being fetched (not just snippets)
- Verify data freshness — is the bot using stale data?
- Check for proper URL handling and deduplication
- Verify search query construction quality
- Check for error handling when Serper returns no results or errors
- Verify caching strategy (are repeated searches cached?)
- Check for any web scraping components and their robustness
- Verify data preprocessing before feeding to LLM

## 6. Trading Logic & Risk Management

- Audit the core trading decision engine:
  - How does it decide when to trade vs when to skip?
  - What's the edge threshold? (e.g., only trade when forecast differs from market by X%)
  - Is there a minimum confidence requirement?
- Check position sizing logic:
  - Fixed size or Kelly criterion or other method?
  - Maximum position size limits?
  - Maximum portfolio exposure limits?
- Verify risk management:
  - Stop loss mechanisms
  - Maximum daily loss limits
  - Maximum number of concurrent positions
  - Drawdown protection
  - Circuit breakers (halt trading if too many losses)
- Check for proper handling of partial fills
- Verify P&L tracking accuracy
- Check for any market manipulation risks (wash trading, spoofing)
- Verify the bot doesn't trade on markets it shouldn't (restricted categories, etc.)

## 7. Backtesting & Performance Tracking

- Audit backtest infrastructure:
  - Historical data sourcing and accuracy
  - Backtest methodology (walk-forward, out-of-sample, etc.)
  - Are backtest results realistic (accounting for slippage, fees, market impact)?
- Check calibration tracking:
  - Brier score calculation
  - Calibration curve generation
  - Performance by market category
  - Performance by probability bucket (extreme vs uncertain)
- Verify logging of all trading decisions:
  - Forecast vs actual outcome
  - Entry price, exit price, P&L
  - Model used, prompt version, data sources
  - Timestamp accuracy
- Check for any selection bias in performance reporting

## 8. Error Handling & Reliability

- Check EVERY try/except block — are errors properly logged? Silently swallowed?
- Verify retry logic for ALL external API calls (Kalshi, Anthropic, Serper)
- Check for proper timeout configuration on all HTTP requests
- Verify graceful degradation:
  - What happens if Anthropic API is down?
  - What happens if Kalshi API is down?
  - What happens if Serper is down?
  - What happens if the internet connection drops mid-trade?
- Check for race conditions in async operations
- Verify pm2 restart behavior — does the bot recover cleanly from crashes?
- Check for proper state persistence — if the bot restarts, does it know its open positions?
- Verify logging captures enough detail to diagnose production issues
- Check for memory leaks (growing lists, unclosed connections, accumulated data)
- Verify the bot can handle Kalshi API changes gracefully

## 9. Security Review

- Check for exposed credentials in any file (grep for key patterns: sk-ant, KALSHI, api_key, secret, password)
- Verify .gitignore covers all sensitive files
- Check that API keys are stored securely (env vars, not config files)
- Verify no customer/account data is logged in plaintext
- Check for command injection risks in any subprocess calls
- Verify HTTPS is used for all API calls
- Check for any local file storage that contains sensitive data
- Verify that API rate limiting doesn't expose timing information

## 10. Code Quality

- Find functions over 50 lines (Python functions should be shorter than JS)
- Find files over 300 lines (candidates for splitting)
- Check for TODO/FIXME/HACK/XXX comments — list them ALL with context
- Check for bare except clauses (should catch specific exceptions)
- Check for mutable default arguments
- Verify type hints on all function signatures
- Check for f-string vs .format() consistency
- Look for magic numbers that should be named constants
- Check for proper docstrings on all public functions
- Verify logging uses proper log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Check for any print() statements that should be logging
- Look for copy-pasted code blocks that should be shared utilities
- Check imports are organized (stdlib, third-party, local)

## 11. Regulatory Compliance

- Verify the bot only trades on Kalshi (CFTC-regulated, legal for US users)
- Check that no Polymarket integration exists (not legal for US residents)
- Verify there's no attempt to circumvent Kalshi's terms of service
- Check position limits compliance
- Verify the bot doesn't attempt to manipulate markets
- Check for proper record-keeping of all trades (required for tax reporting)

## 12. Improvement Roadmap Audit

Based on the known roadmap, check implementation status of:
- [ ] Feeding Kalshi market price into Claude's prompt
- [ ] GPT-4o as second forecaster for ensemble averaging
- [ ] Superforecaster-style prompt decomposition
- [ ] Fetching full article text from Serper results
- [ ] Multi-model ensemble with disagreement handling
- [ ] Calibration tracking with Brier scores
- [ ] Performance dashboard

## Output Format

Produce a structured report with:

### Summary Dashboard
- Total source files, total lines of code, total test count
- External API integrations (count and status)
- Trading statistics if available (total trades, win rate, P&L)
- Env var count (total, documented, undocumented)
- Test coverage estimate (by module)

### Issues by Severity

**🔴 CRITICAL** (could lose money, security holes, regulatory issues) — FIX BEFORE NEXT TRADE
**🟠 HIGH** (reliability issues, missing error handling, data accuracy) — FIX THIS WEEK
**🟡 MEDIUM** (code quality, missing tests, performance) — FIX WHEN POSSIBLE
**🟢 LOW** (style, documentation, minor improvements) — OPTIONAL

For EVERY issue include:
- File path and line number(s)
- What's wrong (specific, not vague)
- Impact (what could go wrong — in dollars if applicable)
- Suggested fix (actionable, 1-3 sentences)

### API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | | | | | | | |
| Kalshi WebSocket | | | | | | | |
| Anthropic (Claude) | | | | | | | |
| Serper (Search) | | | | | | | |

### Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | | | | |
| Forecast Generation | | | | |
| Edge Detection | | | | |
| Position Sizing | | | | |
| Order Execution | | | | |
| Position Tracking | | | | |
| P&L Calculation | | | | |
| Settlement Handling | | | | |

### Module-by-Module Scorecard

For each module/file, rate 1-5:
- Code quality
- Test coverage
- Error handling
- Risk controls
- Documentation
- Overall health

### Top 10 Recommendations (Prioritized)

The single most impactful improvements ranked by:
1. Risk reduction (could this lose money?)
2. Reliability (could this cause missed trades or phantom trades?)
3. Performance (could this improve returns?)
4. Code quality (maintainability)

---

**CRITICAL REMINDER**: This system trades real money on regulated markets. A bug in order placement could result in unintended positions worth hundreds or thousands of dollars. A bug in position tracking could lead to over-leveraging. A bug in the forecasting pipeline could lead to systematically bad trades. Audit with the same rigor you would apply to a financial system — because that's what this is.

Be thorough. Read EVERY file. Check EVERY function. This audit should be exhaustive.
