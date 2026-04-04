# PolyEdge — AI-Driven Prediction Market Trading Bot

**Author:** Adam (VAYU / IO Distributors LLC) | **Created:** March 12, 2026 | **Platform:** Python 3.12+ on Mac Mini M4 Pro

## 1. PROJECT OVERVIEW

PolyEdge runs 7+ complementary strategies simultaneously on Polymarket:
1. **AI Probability** — Claude estimates true probabilities, trades mispriced markets
2. **Cross-Market Logical Arb** — Pricing inconsistencies between related markets (same platform)
3. **Cross-Platform Arb** — Kalshi vs Polymarket discrepancies (min similarity 0.55)
4. **Whale Tracking** — Monitors proven wallets, trades on 80%+ basket consensus
5. **News-Reactive** — Breaking news → Claude impact assessment → trade before repricing
6. **Late Resolution** — Markets resolving <6h where outcome >90% certain but priced <80%
7. **Mean Reversion** — Fades >10% moves in 2h, max 2% bankroll, auto-closes in 4h

**Why it wins:** Fee-free event markets only | information edge (not speed) | multi-strategy diversification | paper-trade-first discipline | self-calibrating

**Revenue target:** Conservative 5–10% monthly, aggressive 15–30%. Top 1% Polymarket traders have 55–67% win rates — edge comes from SIZING, not accuracy.

## 2. RESEARCH-BACKED DESIGN DECISIONS

| Decision | Rationale |
|----------|-----------|
| **Python** | py-clob-client (v0.34.6, 884★) is most mature SDK; Anthropic/ML libs are Python-native; bridge to VAYU Node.js via HTTP |
| **Event markets only** | Zero fees + info edge viable. Crypto: 3% taker fees, HFT-dominated (500ms buffer removed Feb 2026). Sports: fees rolling out |
| **Half-Kelly sizing** | 75% of full Kelly growth, 75% less variance. Caps: 5%/position, 40% total, 20% correlated |
| **Maker orders preferred** | Zero fees on event markets, rebates on fee-enabled. Worse fill rate but dramatically better economics |
| **Multi-model ensemble** | claude-sonnet-4-6 (routine) + claude-opus-4-6 (positions >$50). Ensemble outperforms single model |
| **SQLite + WAL** | Zero-config, concurrent reads during writes, sufficient for hundreds of trades/day. Upgrade path to Postgres |

**Key research findings:** Only 7.6% of wallets profitable (55–67% win rates) | $2M lost at 51% win rate from bad sizing | $40M arb profits extracted in one year (86M trade study) | Logical cross-market arb persists minutes-to-hours (vs 2.7s for YES/NO rebalancing) | Best wallets specialize in 1–2 categories | Polymarket acquired Dome; $POLY airdrop potential

**Target categories (priority):** Politics/Policy, Geopolitics, Tech/AI, Macro/Fed, Culture/Entertainment

## 3. ARCHITECTURE

### Directory Structure
```
polyedge/
├── CLAUDE.md
├── config/
│   ├── settings.yaml, .env, .env.example, categories.yaml
├── src/
│   ├── main.py
│   ├── core/
│   │   ├── polymarket_client.py, gamma_client.py, data_api_client.py
│   │   ├── websocket_client.py, models.py
│   ├── strategies/
│   │   ├── ai_probability.py, cross_arb.py, cross_platform_arb.py
│   │   ├── whale_tracker.py, news_reactive.py, obvious_no.py
│   │   ├── late_resolution.py, mean_reversion.py
│   ├── analysis/
│   │   ├── claude_forecaster.py, prompt_templates.py, ensemble.py
│   │   ├── calibration.py, market_classifier.py
│   ├── data/
│   │   ├── market_scanner.py, market_graph.py, news_ingestion.py
│   │   ├── whale_monitor.py, leaderboard.py
│   ├── execution/
│   │   ├── order_builder.py, order_router.py, position_manager.py, fill_tracker.py
│   ├── risk/
│   │   ├── risk_engine.py, kelly_sizer.py, portfolio_risk.py, circuit_breaker.py
│   ├── alerts/
│   │   ├── alert_manager.py, imessage_alert.py, daily_report.py
│   ├── dashboard/
│   │   ├── server.py, templates/, static/
│   └── storage/
│       ├── database.py, models.py, cache.py
├── tests/ (test_core/, test_strategies/, test_analysis/, test_execution/, test_risk/, test_integration/)
├── scripts/ (setup_wallet.py, backfill_markets.py, discover_whales.py, run_backtest.py)
├── data/ (markets.db, chroma/, logs/)
├── requirements.txt, pyproject.toml, Makefile, README.md
```

## 4. PHASE 0: Infrastructure & Wallet Setup
**1–2 hours | Setup only**

Tasks: Install MetaMask → fund with USDC on Polygon ($50–100) → export private key → create `.env` (PRIVATE_KEY, FUNDER_ADDRESS, ANTHROPIC_API_KEY) → test Gamma API read → test py-clob-client L0 (`get_ok()`, `get_server_time()`) → derive API creds → test L2 (`get_balance_allowance()`) → set token allowances if EOA → place $1 test trade → create git repo with `.gitignore`

**Exit:** Can read markets, authenticate, and execute a test trade. `.env` excluded from git.

## 5. PHASE 1: Core Foundation & Market Scanner
**1–2 days | ~800–1200 LOC**

### `config/settings.yaml`
See `config/settings.yaml` for all parameters. Key non-obvious values: `scanning.interval_seconds: 300` | `min_volume_24h: 10000` | `min_liquidity: 5000` | `max_markets: 200` | `min_edge_ai: 0.05` | `min_edge_arb: 0.02` | `kelly_fraction: 0.5` | `daily_loss_limit_pct: 0.10` | `claude.temperature: 0.3` | `claude.highstakes_threshold: 50.0`

### `src/core/models.py` — Pydantic models
- `Market`: id, question, description, category, tokens (YES/NO + token_ids), end_date, volume_24h, liquidity, current_prices, resolution_source, active
- `MarketSnapshot`: market_id, timestamp, yes_price, no_price, spread, volume_1h
- `Signal`: strategy_name, market_id, direction (BUY_YES/BUY_NO), edge, confidence, reasoning, timestamp
- `Order`: id, market_id, token_id, side, price, size, order_type, status, created_at, filled_at
- `Position`: market_id, token_id, side, size, avg_entry_price, current_price, unrealized_pnl
- `Trade`: order_id, market_id, execution_price, size, fee, realized_pnl, timestamp
- `CalibrationRecord`: prediction_id, market_id, predicted_probability, market_price_at_prediction, actual_outcome, resolved_at

### `src/core/gamma_client.py` — Market Discovery (no auth)
- `get_active_markets(limit, offset, tag, active=True)`, `get_market_by_id(condition_id)`, `get_events(limit, offset)`
- Pagination + rate limit handling (exponential backoff), response → `Market` models

### `src/core/polymarket_client.py` — Authenticated CLOB Wrapper
- `get_price(token_id, side)`, `get_midpoint(token_id)`, `get_order_book(token_id)`, `get_balance()`, `get_positions()`
- Init ClobClient from `.env`, retry on 429/5xx, health check via `get_ok()`

### `src/data/market_scanner.py`
- `scan_all_markets()` → `filter_markets()` (volume/liquidity/category) → `rank_markets()` (volume × spread × resolution_score) → `update_database()` → `get_top_opportunities(n=50)`
- Runs on APScheduler every `interval_seconds`

### `src/storage/database.py`
- Tables: markets, market_snapshots, signals, orders, positions, trades, calibration_records, whale_wallets, whale_trades
- WAL mode, version-based migrations, helpers: `upsert_market()`, `log_signal()`, `log_trade()`, `get_active_positions()`

**Tests:** gamma_client, polymarket_client, market_scanner, models, database
**Exit:** Scanner fetches 100+ markets, filters to ~30–60, stores in SQLite. `python -m src.main` shows output.

## 6. PHASE 2: AI Probability Engine
**2–3 days | ~1000–1500 LOC**

### `src/analysis/prompt_templates.py`
Templates per category: POLITICS, FED_MACRO, GEOPOLITICS, TECH_AI, CULTURE, GENERAL. Each includes: role definition, market context, resolution criteria verbatim, required base rates, output JSON (`probability`, `confidence_low/high`, `key_factors_for/against`, `uncertainties`, `reasoning`), calibration instruction.

**Critical prompt rules:** ALWAYS include current market price (enables mispricing assessment) | ALWAYS request base rates | ALWAYS include resolution criteria verbatim | Request confidence intervals | Ask what would change the estimate

### `src/analysis/claude_forecaster.py`
- `assess_market(market, news_context="") -> ForecastResult` — selects template by category, calls Claude (sonnet/opus based on stake), parses JSON, logs prompt+response
- `ForecastResult`: probability, confidence_interval, key_factors, uncertainties, reasoning, model_used, tokens_used, latency_ms

### `src/analysis/ensemble.py`
- `ensemble_forecast(market, forecasts) -> EnsembleForecast`
- Phase 2: simple weighted average of Claude estimate + market price (extremal adjustment)
- Phase 8 upgrade: second model, Brier-score-weighted

### `src/analysis/market_classifier.py`
- `classify_market(market) -> str` — keyword matching on question + Gamma tags → prompt template selection
- Also classifies: is_binary, has_clear_resolution, days_to_resolution, liquidity_tier

### `src/strategies/ai_probability.py`
- `scan_for_opportunities(markets) -> list[Signal]` — classify → get price → Claude forecast → calculate edge (`claude_prob - market_price` for BUY_YES) → generate Signal if `abs(edge) >= min_edge_ai`
- Rate-limit-aware, prioritizes highest-volume markets

### `src/strategies/obvious_no.py` — Low-Risk Base Yield
- Finds absurd markets (YES at $0.01–$0.05), buys NO for guaranteed return at resolution
- Filters: clear resolution within 30 days, $5K+ volume, unambiguous criteria
- Annualized return: `(1 - no_price) / no_price * (365 / days_to_resolution)` — enter only if >20%
- Max 10% bankroll in obvious-no (black swan risk)

**Tests:** prompt_templates, claude_forecaster, market_classifier, ai_probability, obvious_no
**Exit:** Claude returns structured estimates, edge calculator identifies mispriced markets, spot-check 10 random markets for reasonableness.

## 7. PHASE 3: Paper Trading & Calibration
**2–3 days build + 2 weeks running | ~800–1000 LOC**

### `src/execution/order_builder.py`
- `build_limit_order(market, token_id, side, price, size) -> SignedOrder`, `build_market_order(…)`
- Validates balance + risk limits + price sanity, sets feeRateBps (0 for event), defaults to maker GTC

### `src/execution/order_router.py`
- `route_order(order, mode="paper"|"live") -> OrderResult`
- Paper: simulates fill, logs to DB, updates portfolio. Live: submits to CLOB, monitors fill. Both go through risk checks.

### `src/execution/position_manager.py`
- `get_all_positions()`, `update_positions()` (refresh prices/P&L), `get_total_exposure()`, `get_correlated_exposure(market_id)` (via market graph), `should_exit(position) -> (bool, reason)`

### `src/risk/risk_engine.py` — Central Risk Gate (ALL must pass)
1. `check_balance` 2. `check_position_limit` (5%) 3. `check_total_exposure` (40%) 4. `check_correlated_exposure` (20%) 5. `check_daily_loss_limit` 6. `check_market_liquidity` (<2% slippage) 7. `check_existing_position` (no double-entry) 8. `check_edge_minimum` 9. `check_resolution_date` 10. `check_cooldown`
→ `RiskCheckResult(passed, failed_checks, warnings)`

### `src/risk/kelly_sizer.py`
- `calculate_position_size(edge, probability, bankroll, current_exposure) -> float`
- Kelly: `f = (p*b - q) / b`, apply half-Kelly, cap at min(f×bankroll, bankroll×max_position_pct), reduce near exposure limit

### `src/risk/circuit_breaker.py`
- Daily loss > `daily_loss_limit_pct` → halt all trading for day
- 3 consecutive losing days → quarter-Kelly | 5 consecutive → halt + alert
- Manual override to resume

### `src/analysis/calibration.py`
- `log_prediction()`, `resolve_prediction()`, `calculate_brier_score(period)` (0.0 perfect, 0.25 random)
- `get_calibration_chart_data()` (binned predicted vs actual), `get_accuracy_by_category()`, `get_edge_vs_actual()`

### Orchestrator (`src/main.py`)
Initializes all components, runs APScheduler loop: scan markets → filter → run AI + ObviousNO strategies → sort signals by edge → risk check → Kelly size → route order → log calibration

**Tests:** risk_engine, kelly_sizer, circuit_breaker, order_router, calibration, integration_paper

**Exit:** Runs 2+ weeks, 50+ paper trades, Brier <0.20, 55–70% win rate, risk engine blocks oversized, circuit breaker works.

### ⚠️ DECISION GATE: Do NOT proceed to Phase 4 unless:
1. Positive EV over 50+ paper trades
2. Brier score < 0.20
3. No critical bugs
4. Comfortable with sizing/risk params

## 8. PHASE 4: Live Trading
**1–2 days + ongoing | ~300–500 LOC**

### Live Execution (`order_router.py`)
- Submit via `client.create_order()` + `client.post_order()`, monitor fills via `get_order()` or WebSocket
- Handle partial fills, expiry, rejection. Cancel-and-replace stale orders.

### `src/core/websocket_client.py`
- Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe: price updates (all tracked markets) + user channel (fill notifications)
- Auto-reconnect, feed real-time prices to position manager

### Enhanced Position Manager
- Sync with on-chain via `client.get_positions()`, reconcile paper vs actual, track slippage, auto-exit

### Three-Gate Safety
- **Gate 1:** `trading.mode: "live"` in config (not default)
- **Gate 2:** `POLYEDGE_LIVE_ENABLED=true` env var
- **Gate 3:** Manual confirmation on first live trade per session

**Exit:** 5+ live trades with correct fills, position tracking matches on-chain, WebSocket reconnects reliably, three-gate prevents accidental trades.

## 9. PHASE 5: Cross-Market Arbitrage
**3–5 days | ~1500–2000 LOC**

### `src/data/market_graph.py` — ChromaDB Vector Store
- Index markets with e5-large-v2 embeddings (question text + category + end_date + token_ids)
- `find_related_markets(market_id, n=10)`, `build_relationship_graph()` (subsets, supersets, mutual exclusives)

### `src/strategies/cross_arb.py` — Three Types
- **Type A (Intra-Market):** `yes + no < 0.98` → buy both. Fast but lasts ~2.7s.
- **Type B (Logical/Subset):** Claude validates relationships (e.g., "Trump wins" vs "Republican wins"). Persists minutes-to-hours.
- **Type C (Mutual Exclusivity):** Multi-outcome event sum ≠ 100%. Sum >102% → sell overpriced; <98% → buy all.

### Claude Validation
- Before any cross-market arb, Claude validates logical relationship. Prevents false positives from different resolution criteria. Cache validated relationships.

**Tests:** market_graph, cross_arb (all 3 types), arb_validation
**Exit:** 1-5+ opportunities/day, Claude filters false positives, positive paper P&L after 1 week.

## 10. PHASE 6: Whale Signal Tracking
**3–4 days | ~1200–1500 LOC**

### `scripts/discover_whales.py`
- Scrape leaderboard, filter: 50+ trades, 55%+ win rate, positive P&L 90d, event market focus
- Verify wallets aren't correlated → output `config/whale_basket.yaml` (5–10 addresses)

### `src/data/whale_monitor.py`
- Poll Data API: `get_whale_positions(addr)`, `get_whale_recent_trades(addr, hours=24)`
- Detect new entries (position didn't exist → now exists). Rate-limit-aware, stagger calls.

### `src/strategies/whale_tracker.py`
- `check_consensus(market_id)` — signal when 80%+ basket agrees
- Timing weight (early entries = higher confidence), size proportional to whale allocation, dedup (skip if whales in for days)

**Exit:** 5–10 whales monitored, consensus detection works, paper win rate >55% after 1 week.

## 11. PHASE 7: News-Reactive Trading
**3–5 days | ~1500–2000 LOC**

### `src/data/news_ingestion.py`
- RSS: Reuters, AP, Bloomberg, political news. Optional: Twitter/X API ($100/mo). Google News for alerts.
- `get_recent_news(topic, hours=1)`, relevance scoring via keyword match against active markets

### `src/strategies/news_reactive.py`
- High-relevance news → map to markets (keyword + semantic) → Claude impact assessment (sonnet for speed) → output: market, direction, shift estimate, confidence → signal if shift > min_edge
- Target: news-to-signal <30s. Markets take 2–8 min to reprice.

**Exit:** Detects stories <5min of publication, Claude assesses <15s, paper accuracy >60% after 2 weeks.

## 12. PHASE 8: Dashboard, Alerts & Optimization
**3–5 days | ~2000–2500 LOC**

### `src/dashboard/server.py` (FastAPI)
- Portfolio overview, strategy breakdown (per-strategy P&L/win rate/Brier), calibration chart, recent signals, whale feed, scanner results

### `src/alerts/imessage_alert.py`
- Bridge to VAYU iMessage infra via HTTP. Alerts: signal detected, trade executed, daily P&L, circuit breaker. One-click trade URLs.

### `src/alerts/daily_report.py`
- EOD summary: trades, P&L, win rate, biggest win/loss, 7-day Brier, strategy comparison, whale basket. Sent via iMessage at `daily_report_time`.

### Optimization
- Adaptive model weighting by category Brier scores, edge decay tracking, A/B test prompt templates

**Exit:** Dashboard at localhost:8080, iMessage alerts working, daily reports auto-sent, calibration improving.

## 13. TESTING & REFERENCES

**Test pyramid:** 60% unit | 25% integration | 15% E2E. Target 80%+ coverage.
```bash
make test                                          # all
pytest tests/test_core/ -v                         # specific
pytest --cov=src --cov-report=html                 # coverage
pytest tests/test_integration/ -v --run-integration # integration (needs API)
```

### API Endpoints
| API | URL | Auth |
|-----|-----|------|
| Gamma (markets) | `https://gamma-api.polymarket.com` | None |
| CLOB (trading) | `https://clob.polymarket.com` | L1/L2 |
| Data (positions) | `https://data-api.polymarket.com` | None |
| WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | — |
| Leaderboard | `https://polymarket.com/leaderboard/overall/monthly/profit` | — |

### SDKs & References
- py-clob-client: `pip install py-clob-client` (v0.34.6) | [GitHub](https://github.com/Polymarket/py-clob-client) | [Docs](https://docs.polymarket.com)
- Anthropic: `pip install anthropic` | [Docs](https://docs.anthropic.com)
- [Official agents framework](https://github.com/Polymarket/agents) (MIT)
- [Fully autonomous bot](https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot)
- [Cross-platform arb](https://github.com/ImMike/polymarket-arbitrage)
- [Academic paper (86M trades)](https://arxiv.org/abs/2508.03474)

## 14. KNOWN PITFALLS

| Pitfall | Problem | Solution |
|---------|---------|----------|
| Sportsbook mentality | Buying YES at $0.66 "because likely" = bad payoff structure | Only trade with QUANTIFIED edge above threshold |
| Asymmetric sizing | 51% win rate, $2M loss from oversized losers | Half-Kelly proportional to edge, 5% cap, circuit breaker |
| Resolution mismatch | Bet on common understanding ≠ technical resolution criteria | Include resolution criteria verbatim in Claude prompts |
| AI overconfidence | Claude says 70%, all-in, wrong → blown | Half-Kelly, 5% cap, ensemble, auto-reduce if Brier degrades |
| Liquidity illusion | Good price but can't fill without slippage | Risk engine checks depth, max 10% of best bid/ask, prefer maker |
| Black swan on "Obvious NO" | Near-certain market gets shocked | Max 10% bankroll in obvious-no, diversify across many |
| API rate limits | Aggressive scanning → 429s → missed opportunities | Exponential backoff + jitter, cache, use WebSocket |
| Key security | Private key leaked → wallet drained | `.env` gitignored, dedicated wallet, limited funds in hot wallet |

## PHASE DEPENDENCY MAP
```
Phase 0 (Wallet) → Phase 1 (Core) → Phase 2 (AI) → Phase 3 (Paper) ⚠️GATE → Phase 4 (Live)
                                                                          ├→ Phase 5 (Arb)    ┐
                                                                          ├→ Phase 6 (Whales)  ├ parallel
                                                                          ├→ Phase 7 (News)    ┘
                                                                          └→ Phase 8 (Dashboard)
```

## CLAUDE CODE INSTRUCTIONS

1. Read this CLAUDE.md first for full context
2. Follow the directory structure exactly
3. Use Pydantic models for ALL data structures
4. Write tests BEFORE or alongside implementation (not after)
5. Use `async/await` throughout — this is an async system
6. Log everything at DEBUG level during development, INFO in production
7. Never hardcode secrets — always read from environment variables
8. Run `pytest` and `mypy` after each module
9. Commit after each working module with descriptive message

The goal is a system reliable enough to run 24/7 on the Mac Mini alongside VAYU Order Sync, generating returns while you sleep.
