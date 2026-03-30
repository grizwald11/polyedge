# PolyEdge Codebase Audit Report

**Audit Date:** March 30, 2026 (Revision 26 — fresh re-audit)
**Auditor:** Claude Opus 4.6 (automated, line-by-line)
**Codebase:** /Users/adamgrodin/polyedge (commit 3ccc890)
**Platform:** Python 3.12+ on Mac Mini M4 Pro
**Exchange:** Kalshi (primary), Polymarket (secondary, gated)

---

## Summary Dashboard

| Metric | Value |
|--------|-------|
| Source files (src/) | 61 |
| Test files (tests/) | 57 |
| Total source lines | ~18,520 |
| Total test lines | ~15,628 |
| Tests passing | 1,029 (1 flaky, 3 skipped) |
| External API integrations | 7 (Kalshi, Anthropic, Serper, FRED, Metaculus, Manifold, DuckDuckGo) |
| Environment variables | 12 total, 12 documented in .env.example |
| Trading mode | Paper (live gates disabled) |
| Kalshi API mode | Demo (use_demo: true) |
| Bankroll (config) | $500 |
| Dependencies (pinned) | 16 direct, all exact versions |

---

## Issues by Severity

### CRITICAL (0 issues)

No critical issues found. No bugs that would cause immediate capital loss in production.

---

### HIGH (7 issues)

**H-1: P&L calculations use float arithmetic instead of Decimal**
- **File:** `src/execution/position_manager.py:168-173`
- **What:** Realized P&L computed as `(trade.price - existing.avg_entry_price) * sell_size - proportional_buy_fee - trade.fee` using float, not Decimal. Fee calculations correctly use Decimal (models.py:49-70), but the final P&L subtraction reverts to float.
- **Impact:** Rounding errors accumulate over many trades. Over 1,000 trades, could drift by $0.10-$1.00 — not catastrophic but incorrect for a financial system.
- **Fix:** Use `Decimal(str(...))` for all P&L arithmetic in `record_trade()` and `record_settlement()`. Round final result to 4 decimal places via Decimal quantize.

**H-2: No automatic order cancellation on market close**
- **File:** `src/core/websocket_client.py:381-385`
- **What:** When WebSocket receives a market lifecycle "closed" event, the handler logs a warning but does NOT cancel resting orders on that market. If the order executor doesn't cancel within seconds, an order could execute on a closing market.
- **Impact:** Resting limit orders could fill at stale prices on a closing market, creating unintended positions.
- **Fix:** Add a callback from the lifecycle handler that calls `kalshi_client.cancel_order()` for any pending orders on the closed market.

**H-3: Missing retry logic on secondary data APIs**
- **Files:** `src/data/fred_client.py`, `src/data/metaculus_client.py`, `src/data/data_enricher.py`
- **What:** FRED, Metaculus, and Serper API calls have no retry logic on transient failures (timeouts, 5xx). A single network hiccup causes immediate data loss for that cycle.
- **Impact:** News/economic context lost on transient failures, degrading forecast quality. Kalshi and Claude clients both have proper retry — these are gaps.
- **Fix:** Implement a shared `_request_with_retry(url, max_retries=2, timeout=10)` async helper. Apply to all httpx calls in data clients.

**H-4: Backtesting lookahead bias not mitigated**
- **File:** `scripts/backtest_engine.py:181-202`
- **What:** `MockForecaster` in outcome-derived mode generates synthetic forecasts using knowledge of the actual settlement outcome. The `used_lookahead` flag is set and a degradation multiplier is halved, but there is no option to run backtests without any lookahead.
- **Impact:** Backtest results overestimate live performance. The degradation multiplier (0.5-0.85) is arbitrary with no empirical basis.
- **Fix:** Add a `cached_predictions_only` mode that refuses to generate synthetic forecasts and only replays actual logged predictions. Mark outcome-derived results as "oracle upper bound" in all output.

**H-5: Unresolved backtest positions closed at last-known price**
- **File:** `scripts/backtest_engine.py:551-576`
- **What:** Positions on markets that haven't resolved are closed at the last-known YES/NO price, assuming a favorable exit. In reality, the market may resolve unfavorably or be illiquid.
- **Impact:** Backtest P&L overstated for unresolved positions. Could mask negative expected value.
- **Fix:** Either (a) exclude unresolved positions from return calculations entirely, or (b) resolve at worst-case (0.0 for BUY_YES, 1.0 for BUY_NO) to bound upside bias.

**H-6: Wash trading not prevented**
- **File:** `src/risk/risk_engine.py`
- **What:** No check prevents buying YES and then immediately selling YES (or buying both sides) on the same market in quick succession. The existing position check (line 251-269) blocks double-entry but not rapid buy-then-sell cycles.
- **Impact:** Could generate false profit from spread trades or trigger exchange ToS violations.
- **Fix:** Add a cooldown check in the risk engine: block trades on a market within N minutes of the last exit, or block opposite-side entry within the same cycle.

**H-7: Backtest execution model uses arbitrary fill rates**
- **File:** `scripts/backtest_engine.py:490-497`
- **What:** A hardcoded 15% order miss rate and random 40-80% partial fill for large orders (>50 contracts) are used. These numbers are not calibrated to historical Kalshi fill data.
- **Impact:** Backtest results may significantly over- or under-estimate actual fill rates, making strategy comparisons unreliable.
- **Fix:** Calibrate miss rates and partial fill percentages from historical Kalshi trade data, or parameterize them with sensitivity analysis.

---

### MEDIUM (8 issues)

**M-1: Stale price data accepted for position updates**
- **File:** `src/execution/position_manager.py:221-240`
- **What:** Position price updates are accepted even when WebSocket data is >5 minutes stale and price is unchanged. A `_price_stale` flag is set but the update still proceeds.
- **Impact:** Exit decisions (stop-loss, trailing stop) could fire on stale prices during WebSocket outages.
- **Fix:** Skip price updates when data is >5 min stale AND price unchanged. Only use REST-fetched prices for exit decisions during WebSocket outage.

**M-2: Prose fallback probability extraction could extract wrong number**
- **File:** `src/analysis/claude_forecaster.py:820-844`
- **What:** When JSON parsing fails, the last-match heuristic extracts the final decimal/percentage from Claude's prose response. Complex sentences like "probability was 65% but could be as low as 30%" would extract 30%.
- **Impact:** Incorrect probability → wrong trade direction or edge calculation. Mitigated by `parse_failed=True` flag which downstream filters use to skip the market, but not all code paths check this flag.
- **Fix:** Validate the extracted number against the confidence interval if available, or require the number appear in a "probability" or "estimate" sentence context.

**M-3: Daily loss limit uses 50% weighting on unrealized P&L**
- **File:** `src/risk/circuit_breaker.py:87-98`
- **What:** The daily loss check uses `realized_pnl + (unrealized_pnl * 0.5)`. The 50% discount on unrealized losses can be too lenient when positions are deeply underwater.
- **Impact:** With $50 daily limit: -$60 realized + -$80 unrealized (weighted to -$40) = -$100 total, which exceeds the -$50 threshold. But -$30 realized + -$80 unrealized (weighted to -$40) = -$70, which doesn't trigger. Large unrealized losses could go unaddressed.
- **Fix:** Consider 75% weighting, or add a separate hard gate on unrealized-only losses (e.g., halt if unrealized alone exceeds 15% of bankroll).

**M-4: Brier score input validation weak**
- **File:** `src/analysis/calibration.py:145-160`
- **What:** `calculate_brier_score()` does not validate that `predicted_probability` and `actual_outcome` from the database are in [0, 1] range before computing. Invalid values would silently corrupt the Brier score.
- **Impact:** A single bad record (e.g., probability=1.5 from a parsing error) could skew the Brier score, leading to incorrect calibration multipliers and position sizing.
- **Fix:** Add bounds check: skip records where predicted or actual is outside [0.0, 1.0] with a warning log.

**M-5: Memory growth in long-running bot**
- **Files:** `src/execution/fill_tracker.py` (`_partial_recorded` dict), `src/execution/order_router.py` (`_pending_orders` dict)
- **What:** `_partial_recorded` grows indefinitely (one entry per order with partial fills). `_pending_orders` is not cleaned up if orders disappear from Kalshi (expiry, manual cancel outside bot).
- **Impact:** After 30+ days of continuous operation with thousands of orders, these dicts could consume 10-50MB of memory.
- **Fix:** Periodically prune `_partial_recorded` entries for orders that are no longer pending. Add a cleanup sweep for `_pending_orders` that removes entries older than 24 hours.

**M-6: All news backends fail → zero context for Claude**
- **File:** `src/analysis/news_researcher.py:686-697`
- **What:** When both DuckDuckGo and Serper search backends are unavailable, `get_context()` returns an empty string. Claude then assesses markets with zero news context.
- **Impact:** Increased false-signal probability, especially for news-sensitive markets. The system continues trading rather than degrading gracefully.
- **Fix:** When all backends fail, either (a) decline to assess news-sensitive markets (Fed, Geopolitics), or (b) fall back to cached recent news from the last successful fetch.

**M-7: No out-of-sample backtest validation**
- **File:** `scripts/backtest_engine.py`
- **What:** The `parameter_sweep(cross_validate=True)` option is mentioned but not fully implemented as true walk-forward validation. All data is used chronologically without a holdout set.
- **Impact:** Parameter optimization may overfit to the specific time period. No way to measure generalization.
- **Fix:** Implement rolling-window walk-forward validation: train on months 1-3, test on month 4; train on months 2-4, test on month 5; etc.

**M-8: Backtest results missing category-level metrics**
- **File:** `scripts/backtest_engine.py:82-114`
- **What:** `BacktestResult` stores only aggregate metrics (total P&L, win rate, Sharpe). No per-category breakdown of P&L, win rate, or Brier score.
- **Impact:** Cannot identify which market categories are profitable vs. unprofitable. All categories treated equally in strategy evaluation.
- **Fix:** Add `category_metrics: dict[str, dict]` to `BacktestResult` with per-category P&L, win rate, and trade count.

---

### LOW (5 issues)

**L-1: Circuit breaker resets on ANY successful request**
- **File:** `src/core/kalshi_client.py:266-268`
- **What:** After 5 consecutive 5xx errors trigger the circuit breaker, a single successful request resets the counter to 0. The system could yo-yo between open/closed states during partial API outages.
- **Fix:** Consider requiring 3 consecutive successes before full reset (half-open state pattern).

**L-2: Category-specific divergence thresholds hardcoded**
- **File:** `src/strategies/ai_probability.py:299-304`
- **What:** Max divergence thresholds per category (Politics: 30%, Culture: 50%, etc.) are hardcoded in the strategy, not in config.yaml.
- **Fix:** Move to `config.py` for easier tuning without code changes.

**L-3: Paper mode fill price not clamped to valid range**
- **File:** `src/execution/order_router.py:171-196`
- **What:** Paper trading simulates slippage but doesn't validate the resulting fill price is within [0.01, 0.99]. Could generate unrealistic fills at extreme prices.
- **Fix:** Clamp `simulated_fill_price = max(0.01, min(0.99, fill_price))`.

**L-4: Anthropic client never explicitly closed**
- **File:** `src/analysis/claude_forecaster.py`
- **What:** The `AsyncAnthropic` client is created once and never closed on shutdown. Idle connections may timeout server-side.
- **Fix:** Add `async def close()` to `ClaudeForecaster` and call it from the shutdown handler in lifecycle.py.

**L-5: Dashboard API key comparison not timing-safe**
- **File:** `src/dashboard/server.py:110`
- **What:** Dashboard key comparison uses `!=` operator instead of `hmac.compare_digest()`. Theoretically vulnerable to timing attacks.
- **Impact:** Extremely low risk — dashboard is localhost-only by default and key is optional.
- **Fix:** Replace `provided_key != _dashboard_key` with `not hmac.compare_digest(provided_key, _dashboard_key)`.

---

## API Integration Health Matrix

| Integration | Auth | Error Handling | Retry Logic | Rate Limiting | Timeout Config | Tests | Status |
|---|---|---|---|---|---|---|---|
| Kalshi REST | RSA-PSS SHA-256 | All endpoints wrapped; specific exceptions | 3 retries + exponential backoff (cap 10s) | Retry-After parsing (numeric + HTTP-date); semaphore(5); 100ms min interval | 30s httpx timeout | 177+ tests | Production-ready |
| Kalshi WebSocket | RSA-PSS SHA-256 | Auto-reconnect; auth failure detection; 10-failure permanent halt | Exponential backoff (1s-60s); subscription retry 3x | Ping interval 20s; timeout 30s | Configurable ping/pong | 30+ tests | Production-ready |
| Anthropic (Claude) | API key (env var) | Circuit breaker (3 failures = 5min halt); 4-strategy response parsing | Rate limit: 3 retries; Connection: 3 retries; Auth: no retry | Budget tracking (500K soft / 1M hard daily) | 60s configurable | 35+ tests | Production-ready |
| Serper (Search) | API key (env var) | 3-failure permanent disable; key rotation detection | 2 retries on 5xx; exponential backoff on 429 (cap 30s) | 1-hour cooldown after auth failure | Via httpx | 20+ tests | Production-ready |
| DuckDuckGo | None (free) | Falls back to text search if news search fails | Executor timeout 8s | N/A (free tier) | 8s via asyncio.wait_for | 15+ tests | Production-ready |
| FRED | API key (env var) | Graceful degradation (optional) | **None (H-3)** | N/A | 5s via data_enricher | 10+ tests | **Needs retry** |
| Metaculus | Bearer token (env var) | Graceful degradation (optional) | **None (H-3)** | N/A | 4s via data_enricher | 5+ tests | **Needs retry** |

---

## Trading Logic Scorecard

| Component | Implementation | Tests | Risk Controls | Status |
|---|---|---|---|---|
| Market Discovery | Kalshi /events API with pagination; volume/liquidity/category filters; opportunity scoring | 30+ tests | Category exclusions (Crypto, Sports); min volume $100; min liquidity $100 | Production-ready |
| Forecast Generation | Claude Sonnet-4.6 (routine) / Opus-4.6 (>$50); superforecaster decomposition; 7 category templates; 3-layer prompt injection defense | 35+ tests | Circuit breaker; daily token budget; category-specific temperatures | Production-ready |
| Edge Detection | `claude_prob - market_price`; per-strategy min edge (5% AI, 2% arb, 1% obvious-no, 3% news); calibration bias correction | 25+ tests | Max divergence gate (40%); parse_failed rejection; category Brier gating (>0.30 = halt) | Production-ready |
| Position Sizing | Half-Kelly with calibration multiplier; fee-aware binary search; liquidity adjustment | 50 tests | 5% per position; 40% total exposure; price tier floors ($0.03 reject); Brier-based sizing multiplier | Production-ready |
| Order Execution | Paper (simulated fills) + Live (Kalshi API); maker preferred; timeout reconciliation | 70 tests | Three-gate safety (config + env + session); balance pre-flight; Polymarket residency gate | Production-ready |
| Position Tracking | Weighted avg entry; proportional fee allocation; settlement with fee ledger closure; Kalshi sync | 36 tests | Size clamping on oversells; 4-decimal rounding; synthetic settlement trades | Production-ready |
| P&L Calculation | Realized = (exit - entry) * size - buy_fees - sell_fees; Unrealized = (current - entry) * size; direction-aware | 36 tests | Proportional buy fee allocation; rounding drift prevention | **Float arithmetic (H-1)** |
| Settlement Handling | WebSocket lifecycle events; settlement value validation [0,1]; binary-only enforcement | 10+ tests | Rejects non-binary settlements; synthetic SELL trade with fee closure | Production-ready |

---

## Module-by-Module Scorecard

| Module | Code Quality | Test Coverage | Error Handling | Risk Controls | Documentation | Overall |
|---|---|---|---|---|---|---|
| **src/core/kalshi_client.py** | 5 | 5 | 5 | 5 | 5 | 5 |
| **src/core/websocket_client.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/core/models.py** | 5 | 5 | 4 | N/A | 5 | 5 |
| **src/core/market_discovery.py** | 4 | 4 | 4 | 3 | 4 | 4 |
| **src/core/key_loader.py** | 5 | 4 | 4 | N/A | 4 | 4 |
| **src/core/polymarket_client.py** | 4 | 1 | 4 | 3 | 4 | 3 |
| **src/core/polymarket_discovery.py** | 4 | 3 | 4 | 3 | 4 | 3 |
| **src/analysis/claude_forecaster.py** | 4 | 4 | 5 | 5 | 4 | 4 |
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
| **src/data/fred_client.py** | 4 | 3 | 3 | N/A | 4 | 3 |
| **src/data/whale_monitor.py** | 4 | 3 | 4 | 3 | 4 | 4 |
| **src/data/market_graph.py** | 3 | 3 | 3 | N/A | 3 | 3 |
| **src/data/manifold_client.py** | 4 | 4 | 4 | N/A | 4 | 4 |
| **src/data/metaculus_client.py** | 4 | 3 | 3 | N/A | 4 | 3 |
| **src/data/polymarket_cross_ref.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/strategies/ai_probability.py** | 4 | 4 | 4 | 5 | 4 | 4 |
| **src/strategies/cross_arb.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/cross_platform_arb.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/obvious_no.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/whale_tracker.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/strategies/news_reactive.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/execution/order_builder.py** | 5 | 4 | 4 | 4 | 5 | 4 |
| **src/execution/order_router.py** | 4 | 4 | 4 | 5 | 4 | 4 |
| **src/execution/position_manager.py** | 4 | 5 | 4 | 5 | 4 | 4 |
| **src/execution/fill_tracker.py** | 4 | 4 | 5 | 4 | 4 | 4 |
| **src/risk/risk_engine.py** | 4 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/kelly_sizer.py** | 4 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/circuit_breaker.py** | 5 | 5 | 4 | 5 | 4 | 5 |
| **src/risk/portfolio_risk.py** | 4 | 3 | 4 | 4 | 4 | 4 |
| **src/risk/manipulation_detector.py** | 4 | 4 | 4 | 4 | 4 | 4 |
| **src/storage/database.py** | 3 | 4 | 4 | 3 | 4 | 3 |
| **src/dashboard/server.py** | 4 | 4 | 4 | 4 | 3 | 4 |
| **src/alerts/alert_manager.py** | 4 | 2 | 4 | N/A | 4 | 3 |
| **src/alerts/daily_report.py** | 4 | 3 | 4 | N/A | 4 | 4 |
| **src/main.py** | 5 | 4 | 4 | 5 | 4 | 5 |
| **src/orchestrator/startup.py** | 5 | 4 | 4 | N/A | 4 | 4 |
| **src/orchestrator/scan_cycle.py** | 4 | 4 | 4 | 5 | 4 | 4 |
| **src/orchestrator/trade_cycle.py** | 4 | 4 | 4 | 5 | 4 | 4 |
| **src/orchestrator/lifecycle.py** | 4 | 4 | 4 | 5 | 4 | 4 |
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
├── requirements.txt                   # 16 pinned dependencies
├── ecosystem.config.js                # PM2 process manager config
├── Makefile                           # Build automation
├── .gitignore                         # Secrets, DB, venv excluded
├── config/
│   ├── settings.yaml                  # Main configuration (101 lines)
│   ├── categories.yaml                # Market category mappings (51 lines)
│   ├── .env                           # Secrets (NOT in git)
│   ├── .env.example                   # Template (in git, 42 lines)
│   └── kalshi_private_key.pem         # RSA key (NOT in git)
├── src/                               # 61 Python files, ~18,520 lines
│   ├── main.py                        # Thin entry point (28 lines)
│   ├── config.py                      # Pydantic settings loader (282 lines)
│   ├── metrics.py                     # Performance metrics (226 lines)
│   ├── orchestrator/   (5 files)      # Main loop: startup, scan, trade, lifecycle
│   ├── core/           (8 files)      # API clients, data models, key loader
│   ├── analysis/       (9 files)      # Claude forecasting, calibration
│   ├── data/           (14 files)     # Market scanning, news, whales
│   ├── strategies/     (6 files)      # 5 trading strategies
│   ├── execution/      (5 files)      # Order routing, position tracking
│   ├── risk/           (6 files)      # Risk engine, circuit breaker
│   ├── storage/        (2 files)      # SQLite database
│   ├── dashboard/      (5 files)      # FastAPI web UI
│   └── alerts/         (4 files)      # Alert dispatch, iMessage
├── tests/                             # 57 Python files, ~15,628 lines
│   ├── conftest.py                    # Shared fixtures (240 lines)
│   ├── test_core/      (8 files)
│   ├── test_analysis/  (8 files)
│   ├── test_data/      (13 files)
│   ├── test_execution/ (5 files)
│   ├── test_risk/      (5 files)
│   ├── test_strategies/(6 files)
│   ├── test_scripts/   (3 files)
│   ├── test_dashboard/ (2 files)
│   ├── test_integration/(1 file)
│   └── test_alerts/    (3 files)
├── scripts/                           # Utility scripts
│   ├── backtest_engine.py             # Strategy replay (915 lines)
│   ├── run_backtest.py                # Post-hoc analysis (270 lines)
│   ├── backfill_markets.py            # Historical data loader (345 lines)
│   ├── discover_whales.py             # Whale basket builder (71 lines)
│   └── leaderboard.py                 # Whale discovery (65 lines)
└── data/                              # Runtime (NOT in git)
    ├── markets.db
    ├── chroma/
    └── logs/
```

### Orphaned / Dead Code
No orphaned files detected. All 61 source modules are imported by at least one other module or test file.

### Config Files
- **pyproject.toml:** Present. Requires Python >=3.12. Pytest and MyPy configured.
- **requirements.txt:** Present. 16 dependencies, ALL pinned to exact versions. No known CVEs.
- **ecosystem.config.js:** Present. Runs `venv/bin/python -m src.main`. Autorestart enabled, max 15 restarts, 60s kill timeout, 500MB memory limit.
- **.gitignore:** Present. Covers `.env`, `*.pem`, `*.key`, `data/*.db`, `venv/`, `__pycache__/`.
- **.env.example:** Present in `config/`. Documents all 12 environment variables.

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

No known CVEs detected. No unused dependencies found.

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
| `CONFIRM_NON_US_POLYMARKET` | No | order_router.py:204,569 | Yes |
| `POLYEDGE_DASHBOARD_KEY` | No | dashboard/server.py:85-90 | Yes |
| `POLYEDGE_CORS_ORIGINS` | No | dashboard/server.py:72 | Yes |

All 12 variables documented in `.env.example`. Zero undocumented variables.

### Hardcoded Values
No API keys or secrets hardcoded. All endpoints configurable via `config/settings.yaml`. Kalshi supports demo/production toggle (`use_demo: true/false`).

### Secrets in Git
Confirmed: No secrets in git history (`git log --all --diff-filter=A -- '*.env' '*.pem' '*.key'` returns empty). `.gitignore` properly covers all sensitive files.

### RSA Key Security
- Key file permissions enforced at 0o600 (key_loader.py:38-51)
- Auto-fixed with warning if permissions are too permissive
- Key freshness checking via mtime comparison (kalshi_client.py:83-115) — supports key rotation without restart

---

## Section 3: Kalshi Integration

### Endpoints Used (12 total)
**Public (6):** `/exchange/status`, `/markets`, `/markets/{ticker}`, `/events`, `/markets/{ticker}/orderbook`, `/markets/trades`
**Authenticated (6):** `/portfolio/balance`, `/portfolio/positions`, `/portfolio/orders` (GET/POST), `/portfolio/orders/{id}` (GET/DELETE)
**WebSocket:** `wss://demo-api.kalshi.co/trade-api/ws/v2` (channels: ticker, fill, market_lifecycle_v2)

### Authentication
- RSA-PSS with SHA-256 signing, base64 encoded
- Key file permissions enforced (0o600), auto-fixed with warning
- Timestamp in milliseconds prevents replay attacks
- Single auth retry on 401/403 with 2s backoff
- WebSocket uses same signing mechanism with per-connection headers

### Rate Limiting
- Parses `Retry-After` header (both numeric seconds and HTTP-date format)
- Exponential backoff fallback: 2^attempt with jitter, capped at 10s
- Concurrency semaphore: max 5 simultaneous requests
- Minimum request interval: 100ms enforced between all requests
- `KalshiRateLimitError` raised after 3 retries exhausted

### Order Placement
- Price conversion via Decimal: `Decimal(str(dollars)).quantize(Decimal("0.01")) * 100`
- Cents validation: range [1, 99] enforced before submission
- Side stored explicitly in `order.kalshi_side` (no fragile string matching)
- Stale price warning if market data >5 minutes old
- Orphaned order reconciliation on timeout: matches by ticker, price (within 1 cent), side, quantity

### Circuit Breaker
- Opens after 5 consecutive 5xx errors
- Exponential backoff: `min(600, 60 * 2^(triggers-1))` — 60s → 120s → 240s → 480s → 600s max
- Resets on successful request (L-1: consider requiring N consecutive successes)

### Monetary Calculations
- **Fees:** Fully Decimal-based with ROUND_CEILING (models.py:49-70)
- **Order cost:** `round(price * size + fee_dollars, 4)` (order_builder.py:75)
- **P&L:** Float arithmetic (H-1: should use Decimal)

---

## Section 4: AI Forecasting Pipeline

### Claude Integration
- System prompt with superforecaster decomposition (AND/OR/conditional probability)
- 7 category-specific templates (Politics, Fed/Macro, Geopolitics, Tech/AI, Culture, General + fallback)
- Market price included in all prompts: `CURRENT MARKET PRICE: {market_price:.0%} (YES)`
- 3-layer prompt injection defense: control char stripping → injection pattern detection → character allowlist

### Model Selection
- Primary: Claude Sonnet-4.6 (routine assessments)
- High-stakes: Claude Opus-4.6 (positions >$50)
- Category-specific temperatures: Politics 0.25, Fed 0.20, Geo 0.30, Tech 0.30, Culture 0.40

### Response Parsing (4-strategy fallback)
1. Direct JSON parse
2. Markdown code block extraction
3. Brace extraction (first `{` to last `}`)
4. Prose fallback (last decimal/percentage match) — M-2 risk of wrong extraction

### Token Budget
- Soft limit: 500K tokens/day (warning)
- Hard limit: 1M tokens/day (refuses further calls)
- Per-call cost tracking with model-specific pricing

### Ensemble
- Single-model: 85% Claude + 15% market weight (adaptive based on CI width, divergence)
- Multi-model: Brier-score-weighted averaging of Claude + community forecasts (Manifold/Metaculus)
- Disagreement penalty: `confidence *= max(0.3, 1.0 - std_dev)`

### Calibration
- Time-decay Brier scores (30-day half-life)
- Per-category bias corrections (min 15 resolved predictions)
- Category accuracy gating: Brier >0.30 halts trading in that category
- 10-bin calibration curves for visualization

---

## Section 5: Data Pipeline & News Integration

### Search Architecture
- **Primary:** DuckDuckGo (free, no key required)
- **Fallback:** Serper.dev (paid, with sophisticated error recovery)
- Serper permanent disable after 3 consecutive auth failures; auto-recovery on key rotation

### Article Fetching
- Custom HTML parser (`_ArticleTextExtractor`) skips nav/header/footer/script tags
- Top 3 results fetched with full text (5s timeout per article)
- 50-word minimum article filter rejects stubs
- MAX_ARTICLE_CHARS = 3000 per article, MAX_CONTEXT_CHARS = 4000 total

### Data Freshness
- Category-aware staleness: Fed 5 days, Geopolitics 7 days, Politics 14 days, Culture 30 days
- Cache TTLs: News 2 min, Economic 60 min, Community forecasts 30 min
- RSS feed polling with exponential backoff on failures

### Source Trust
- 4-tier reliability multipliers: Reuters/AP 1.3x, NYT/WaPo/BBC/Bloomberg 1.2x, FT/WSJ/Economist 1.15x, NPR/Politico 1.1x
- Applied to relevance scoring before context assembly

### Deduplication
- URL normalization (strip tracking params: utm_*, fbclid, etc.)
- Title-based near-duplicate detection (Jaccard similarity threshold 0.7)
- RSS feed LRU cache (10,000 URLs)

---

## Section 6: Trading Logic & Risk Management

### Pre-Trade Risk Checks (13 total)
1. Balance check — sufficient USDC
2. Position size limit — max 5% of bankroll
3. Total exposure limit — max 40% of bankroll
4. Correlated exposure — max 20% per event
5. Circuit breaker status — must not be halted
6. Market liquidity — order must be <10% of book
7. Existing position — block double-entry (unless additions allowed)
8. Min confidence — must be ≥55%
9. Trade cost — must be >$0
10. Min edge — strategy-specific minimum
11. Resolution date — must be >1 day away
12. Cooldown — 4h after loss, 1h after profit
13. Manipulation detector — flag suspicious markets

### Position Sizing
- Half-Kelly (config `kelly_fraction: 0.5`) applied to raw Kelly fraction → effective quarter-Kelly
- Low-price reject: <$0.03 always rejected, $0.03-$0.10 requires 10% edge
- Liquidity adjustment: halve size if >10% of book, 75% if >5%
- Calibration multiplier: Brier ≤0.10 → 1.1x, ≤0.18 → 1.0x, ≤0.20 → 0.75x, ≤0.25 → 0.50x, ≤0.30 → 0.25x, >0.30 → 0x
- Hard caps: 5% per position, 40% total, 6 max concurrent positions

### Exit Logic (7 conditions)
1. **Stop-loss:** 28% loss (30% - 2% slippage buffer)
2. **Trailing stop:** Activate at 10% gain, trail 50% of peak
3. **Take-profit:** 78% of max theoretical gain
4. **Time-based:** Held >21 days
5. **Expiry:** Market closes <1 day away + position underwater
6. **Edge-gone:** Remaining edge <22% (20% + 2% slippage buffer)
7. **Capital rotation:** Exposure >35% + remaining edge <40% of original

### Circuit Breaker (3 tiers)
- **Tier 1:** 20% max drawdown → permanent halt until manual reset
- **Tier 2:** 10% daily loss → halt for 24 hours (auto-reset)
- **Tier 3:** 3-4 consecutive losing days → quarter-Kelly; 5+ → full halt

### Manipulation Detection
- Rapid price moves: >20% relative change (percentage-based, not absolute)
- Slow drift: monotonic drift >15% cumulative over ≤30 minutes, 3+ snapshots
- Crossed book: YES + NO deviation >5% from 1.0
- Flags expire after 30 minutes; max 50 price history entries per market

---

## Section 7: Backtesting & Performance Tracking

### Historical Data
- Fetched from Kalshi API via `scripts/backfill_markets.py`
- Supports live, settled, and price history modes
- No independent data validation or checksums

### Backtest Methodology
- Walk-forward validation: referenced but not fully implemented (H-4)
- Out-of-sample: not implemented (M-7)
- Lookahead bias: acknowledged via `used_lookahead` flag but not mitigated (H-4)

### Realism
- **Slippage:** Flat 10 bps default; optional depth-aware model (requires liquidity data)
- **Fees:** Uses actual Kalshi fee functions (Decimal-based, correct)
- **Fill rates:** Arbitrary 15% miss rate, not calibrated (H-7)
- **Market impact:** Not modeled
- **Unresolved positions:** Closed at last-known price (H-5)

### Calibration Integration
- Brier score formula correct: `(predicted - actual)^2` with time-decay weighting
- Per-category Brier tracking with 5-record minimum
- 10-bin calibration curves
- Input validation weak (M-4)

---

## Section 8: Error Handling & Reliability

### Exception Handling
- Zero bare `except:` blocks in entire codebase
- All exception handlers specify types
- Some overly broad `except Exception as e` in orchestrator (acceptable for top-level loop)

### Retry Logic Coverage
| Client | Retry | Status |
|--------|-------|--------|
| Kalshi REST | 3 retries + exponential backoff | Production-ready |
| Kalshi WebSocket | Exponential 1-60s, 10-failure halt | Production-ready |
| Claude API | 3 retries for rate limit + connection | Production-ready |
| Serper | 2 retries on 5xx, backoff on 429 | Production-ready |
| DuckDuckGo | Library-level timeout only | Acceptable |
| FRED | **None** | **H-3** |
| Metaculus | **None** | **H-3** |

### Timeout Coverage
- Kalshi REST: 30s httpx timeout
- Claude API: 60s configurable
- Scan cycle: configurable (default 30s)
- Position sync: 10s
- All async operations wrapped in `asyncio.wait_for()`

### Graceful Degradation
- 1-2 strategies fail: continues with remaining (logged as DEGRADED)
- All strategies fail: escalates to CRITICAL alert
- Claude down: returns market price as fallback forecast
- News APIs down: proceeds without news context (M-6)
- WebSocket disconnect: falls back to REST polling

### State Persistence for Crash Recovery
- Pending orders: persisted in `pending_orders` table, restored at startup
- Open positions: persisted in `positions` table, restored via PositionManager
- Bankroll: synced from Kalshi API every cycle
- Calibration data: persisted in `calibration_records` table
- PID lock prevents duplicate instances

### Memory Bounds
- `_processed_fills`: pruned at 10,000 entries
- `_edge_return_log`, `_all_signal_edges`: bounded at 1,000 entries
- `_forecast_cache`: TTL-based eviction
- `_partial_recorded`: **unbounded** (M-5)
- `_pending_orders`: **no automatic cleanup** (M-5)

---

## Section 9: Security Review

### Credential Security
- Zero hardcoded secrets in source code
- All API keys loaded from environment variables via dotenv
- RSA key file permissions enforced at 0o600
- No secrets in git history (verified via git log --all --diff-filter=A)
- .gitignore covers: .env, *.pem, *.key, credentials*.json, data/*.db

### Attack Surface
- No `subprocess`, `os.system()`, `eval()`, or `exec()` calls in src/
- All external APIs use HTTPS
- Dashboard restricted to localhost or API key authenticated
- Prompt injection defense: 3-layer sanitization on all external text

### Logging Safety
- No API keys, secrets, or account data logged in plaintext
- Serper key redacted from error logs
- Log rotation: 10MB max with 5 backups

---

## Section 10: Code Quality

### Quality Metrics
- Zero TODO/FIXME/HACK/XXX comments
- Zero bare `except:` clauses
- Zero mutable default arguments (uses `Field(default_factory=...)`)
- Zero `print()` statements (all output via logging)
- All modules use `from __future__ import annotations`
- Comprehensive type hints on all public functions
- All numeric thresholds named as constants
- Import ordering: `__future__` → stdlib → third-party → local (isort-enforced)
- 99%+ docstring coverage on public methods

### Logging Discipline
- 633 logging calls across 52 files
- Proper level usage: DEBUG (details), INFO (events), WARNING (degraded), ERROR (failures), CRITICAL (halts)
- JSON-structured metrics logging for operational monitoring

---

## Section 11: Regulatory Compliance

### Kalshi (CFTC-Regulated)
- Primary exchange. All order placement routed through authenticated Kalshi API.
- Trade records stored in `trades` table with timestamps, prices, sizes, fees — suitable for tax reporting.
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
- No wash trading, spoofing, or layering mechanisms exist in the code.
- **Gap:** No explicit wash trading prevention check (H-6).

---

## Section 12: Improvement Roadmap Audit

| Feature | Status | Evidence |
|---------|--------|---------|
| Feeding market price into Claude's prompt | DONE | All 7 templates include `CURRENT MARKET PRICE: {market_price:.0%}` |
| GPT-4o as second forecaster | REPLACED | Community forecasts (Manifold/Metaculus) fill this role — lower cost, independent |
| Superforecaster-style prompt decomposition | DONE | System prompt lines 34-40 with AND/OR/conditional breakdown |
| Fetching full article text from search results | DONE | Custom HTML parser in news_researcher.py:22-71; top 3 articles fetched |
| Multi-model ensemble with disagreement handling | DONE | Brier-weighted averaging in ensemble.py; disagreement penalty applied |
| Calibration tracking with Brier scores | DONE | Per-category, time-decay, calibration curves, bias corrections |
| Performance dashboard | DONE | FastAPI at localhost:8080 with portfolio, positions, signals, calibration views |

---

## Top 10 Recommendations (Prioritized)

1. **H-1: Convert P&L calculations to Decimal arithmetic** — Prevents rounding drift on a financial system. Risk: accumulated errors over many trades.
2. **H-2: Auto-cancel resting orders on market close** — Prevents unintended fills on closing markets. Add WebSocket lifecycle callback.
3. **H-3: Add retry logic to FRED/Metaculus clients** — Transient failures currently cause immediate data loss. Use shared retry helper.
4. **H-4: Mitigate backtesting lookahead bias** — Add cached-predictions-only mode. Mark outcome-derived results as upper bounds.
5. **H-5: Fix unresolved backtest position handling** — Close at worst-case or exclude from returns to prevent overstated P&L.
6. **H-6: Add wash trading prevention** — Block same-market opposite-side trades within cooldown window.
7. **H-7: Calibrate backtest fill rates from historical data** — Replace arbitrary 15% miss rate with empirical rates.
8. **M-1: Reject stale price updates for exit decisions** — Prevent false stop-loss/trailing-stop triggers during WebSocket outages.
9. **M-3: Tighten daily loss limit unrealized weighting** — Increase from 50% to 75% or add separate unrealized-only gate.
10. **M-5: Add periodic cleanup for unbounded memory structures** — Prune `_partial_recorded` and `_pending_orders` dicts.

---

*Report generated by Claude Opus 4.6 on March 30, 2026. Revision 26 (fresh re-audit): 1,029 tests passing (1 flaky, 3 skipped). 20 findings identified: 0C + 7H + 8M + 5L.*
