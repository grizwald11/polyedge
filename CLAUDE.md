# POLYMARKET TRADING BOT — Complete Phased Development Plan

## Project: PolyEdge
## Author: Adam (VAYU / IO Distributors LLC)
## Created: March 12, 2026
## Platform: Python 3.12+ on Mac Mini M4 Pro (existing VAYU infrastructure)

---

## TABLE OF CONTENTS

1. [Project Overview](#1-project-overview)
2. [Research-Backed Design Decisions](#2-research-backed-design-decisions)
3. [Master Architecture](#3-master-architecture)
4. [Phase 0: Infrastructure & Wallet Setup](#4-phase-0-infrastructure--wallet-setup)
5. [Phase 1: Core Foundation & Market Scanner](#5-phase-1-core-foundation--market-scanner)
6. [Phase 2: AI Probability Engine](#6-phase-2-ai-probability-engine)
7. [Phase 3: Paper Trading & Calibration](#7-phase-3-paper-trading--calibration)
8. [Phase 4: Live Trading & Risk Engine](#8-phase-4-live-trading--risk-engine)
9. [Phase 5: Cross-Market Arbitrage Scanner](#9-phase-5-cross-market-arbitrage-scanner)
10. [Phase 6: Whale Signal Tracking](#10-phase-6-whale-signal-tracking)
11. [Phase 7: News-Reactive Trading](#11-phase-7-news-reactive-trading)
12. [Phase 8: Dashboard, Alerts & Optimization](#12-phase-8-dashboard-alerts--optimization)
13. [Testing Strategy](#13-testing-strategy)
14. [Key Technical References](#14-key-technical-references)
15. [Known Pitfalls & How to Avoid Them](#15-known-pitfalls--how-to-avoid-them)

---

## 1. PROJECT OVERVIEW

### What This Software Does
PolyEdge is a multi-strategy AI-driven prediction market trading platform that runs 7 complementary strategies simultaneously:
1. **AI Probability Assessment** — Claude analyzes markets, estimates true probabilities, trades when market is mispriced
2. **Cross-Market Logical Arbitrage** — Detects pricing inconsistencies between related markets on the same platform
3. **Cross-Platform Arbitrage** — Detects pricing discrepancies between Kalshi and Polymarket for the same event (requires validated market pairs; min similarity 0.55)
4. **Whale Signal Tracking** — Monitors proven wallets, trades on basket consensus
5. **News-Reactive Trading** — Detects breaking news, assesses impact, trades before crowd reprices
6. **Late Resolution** — Trades markets resolving within 6 hours where public info makes the outcome >90% certain but the market is still priced <80%. Speed advantage on public evidence.
7. **Mean Reversion** — Fades sharp intraday price moves (>10% in 2 hours) with small positions (max 2% bankroll), auto-closes within 4 hours

### Why This Architecture Wins
- **Fee-free markets only** — All strategies target event/political markets (zero fees). Avoids 5/15-min crypto markets where HFT bots dominate.
- **Information edge, not speed edge** — Claude's ability to synthesize 50 news articles in seconds is the competitive advantage. Not latency.
- **Multi-strategy diversification** — Strategies are uncorrelated. When AI probability underperforms, arbitrage and whale signals can still produce returns.
- **Paper-trade-first discipline** — Every strategy must prove positive EV in paper trading before touching real capital.
- **Calibration loop** — The system tracks its own accuracy and improves over time, unlike static bots.

### Revenue Target Model
- Conservative: 5–10% monthly on deployed capital (compounding across 4 strategies)
- Aggressive: 15–30% monthly with full automation and optimized sizing
- Baseline context: Top 1% of Polymarket traders have 55–67% win rates and still generate massive returns through proper sizing. 87% of wallets lose money. We're building the system to be in the 7.6%.

### Key Research Findings Baked Into This Plan
1. Only 7.6% of Polymarket wallets are profitable. The winners have 55–67% win rates (not 80%+). Edge comes from SIZING, not accuracy.
2. A trader lost $2M despite 51% win rate because of asymmetric sizing — buying at $0.51–$0.67 creates worst-case payoff structures.
3. $40M in arbitrage profits were extracted from Polymarket in a single year (academic study of 86M trades).
4. Cross-market logical arbitrage persists MUCH longer than simple YES/NO rebalancing (which lasts ~2.7 seconds).
5. The 500ms taker execution buffer was removed in Feb 2026. Taker fees now apply to crypto/NCAAB/Serie A markets. Event markets remain fee-free.
6. Maker orders earn rebates. Always prefer limit/maker orders.
7. Ensemble AI models (multiple LLMs cross-checked) outperform single-model approaches.
8. "Obvious NO" strategy (buying NO at $0.95–$0.97 on absurd markets) generates low-risk base yield.
9. Best profitable wallets specialize in 1–2 market categories, not generalize.
10. Polymarket acquired Dome (unified API). The $POLY token launch is anticipated — airdrop farming from trading activity may add bonus returns.

---

## 2. RESEARCH-BACKED DESIGN DECISIONS

### Decision: Python over Node.js
- `py-clob-client` (v0.34.6, updated Feb 2026) is Polymarket's most mature SDK with 884 GitHub stars
- Anthropic Python SDK is best-documented
- All ML/data libraries (pandas, numpy, sentence-transformers, chromadb) are Python-native
- Open-source reference bots are overwhelmingly Python
- Bridge to VAYU's Node.js iMessage system via HTTP API later

### Decision: Event Markets Only (No Crypto 5/15-min)
- Event markets: zero fees, information edge viable, Claude's strength
- Crypto markets: taker fees up to 3% at 50% probability, HFT bots with sub-ms latency dominate, removed 500ms buffer kills retail
- Sports markets: fees being rolled out (NCAAB, Serie A), efficient pricing, less AI edge
- Target categories (priority order): Politics/Policy, Geopolitics, Tech/AI, Macro/Fed, Culture/Entertainment

### Decision: Half-Kelly Position Sizing
- Full Kelly is mathematically optimal but produces brutal drawdowns
- Half-Kelly reduces variance by 75% while maintaining 75% of growth rate
- Hard cap: max 5% bankroll per position, max 40% total exposure, max 20% correlated exposure

### Decision: Maker Orders Preferred
- Zero fees on all event markets when using limit (maker) orders
- Earn rebates in fee-enabled markets
- Slightly worse fill rate but dramatically better economics

### Decision: Multi-Model Ensemble for Probability
- Primary: Claude (claude-sonnet-4-6 for speed, claude-opus-4-6 for high-stakes)
- Cross-check: A second probability estimate from a different approach (base rate model, or polling aggregator)
- Calibration chart tracks predicted vs actual to identify systematic bias
- Research shows ensemble approaches outperform any single model

### Decision: SQLite with WAL Mode
- Simple, zero-config, runs locally on Mac Mini
- WAL mode allows concurrent reads during writes
- Sufficient for hundreds of trades/day
- Upgrade path to PostgreSQL if needed later

---

## 3. MASTER ARCHITECTURE

### Directory Structure
```
polyedge/
├── CLAUDE.md                    # This file — project context for Claude Code
├── config/
│   ├── settings.yaml            # All configurable parameters
│   ├── .env                     # Secrets (PRIVATE_KEY, API keys) — NEVER commit
│   ├── .env.example             # Template for .env
│   └── categories.yaml          # Market category definitions and priority weights
├── src/
│   ├── __init__.py
│   ├── main.py                  # Orchestrator — runs all strategies concurrently
│   ├── core/
│   │   ├── __init__.py
│   │   ├── polymarket_client.py # Wrapper around py-clob-client with retry/error handling
│   │   ├── gamma_client.py      # Gamma API for market discovery (no auth needed)
│   │   ├── data_api_client.py   # Data API for positions, wallet tracking
│   │   ├── websocket_client.py  # WebSocket for real-time orderbook/price updates
│   │   └── models.py            # Pydantic models: Market, Position, Order, Trade, Signal
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── ai_probability.py    # Strategy 1: Claude-driven probability assessment
│   │   ├── cross_arb.py         # Strategy 2: Logical arbitrage between related markets
│   │   ├── cross_platform_arb.py # Strategy 3: Kalshi/Polymarket cross-platform pricing arb
│   │   ├── whale_tracker.py     # Strategy 4: Smart money signal tracking
│   │   ├── news_reactive.py     # Strategy 5: Breaking news → rapid trade
│   │   ├── obvious_no.py        # Strategy 6: Low-risk base yield from near-certain markets
│   │   ├── late_resolution.py   # Strategy 7: Trade near-resolution markets with clear outcomes
│   │   └── mean_reversion.py    # Strategy 8: Fade sharp intraday price moves
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── claude_forecaster.py # Claude API integration, prompt construction, parsing
│   │   ├── prompt_templates.py  # Structured prompt templates for each market category
│   │   ├── ensemble.py          # Multi-model probability aggregation
│   │   ├── calibration.py       # Track predicted vs actual, compute Brier scores
│   │   └── market_classifier.py # Classify markets by category, liquidity, suitability
│   ├── data/
│   │   ├── __init__.py
│   │   ├── market_scanner.py    # Poll Gamma API, filter, rank markets by opportunity
│   │   ├── market_graph.py      # ChromaDB vector store for related market detection
│   │   ├── news_ingestion.py    # RSS/API feeds, relevance scoring
│   │   ├── whale_monitor.py     # Wallet monitoring via Data API + Polygon RPC
│   │   └── leaderboard.py       # Leaderboard scraping for whale discovery
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_builder.py     # Build and sign orders via py-clob-client
│   │   ├── order_router.py      # Route to paper or live execution
│   │   ├── position_manager.py  # Track all open positions, P&L, exposure
│   │   └── fill_tracker.py      # Monitor order fills via WebSocket
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── risk_engine.py       # Central risk checks — all trades must pass
│   │   ├── kelly_sizer.py       # Half-Kelly position sizing with caps
│   │   ├── portfolio_risk.py    # Correlation tracking, exposure limits
│   │   └── circuit_breaker.py   # Daily loss limit, auto-halt, cool-down periods
│   ├── alerts/
│   │   ├── __init__.py
│   │   ├── alert_manager.py     # Central alert dispatch
│   │   ├── imessage_alert.py    # iMessage via VAYU infrastructure
│   │   └── daily_report.py      # End-of-day P&L summary
│   ├── dashboard/
│   │   ├── __init__.py
│   │   ├── server.py            # FastAPI dashboard server
│   │   ├── templates/           # Jinja2 HTML templates
│   │   └── static/              # CSS/JS for dashboard
│   └── storage/
│       ├── __init__.py
│       ├── database.py          # SQLite with WAL, migrations
│       ├── models.py            # SQLAlchemy/Peewee ORM models
│       └── cache.py             # In-memory cache for hot data
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Shared fixtures
│   ├── test_core/               # API client tests (mocked)
│   ├── test_strategies/         # Strategy logic tests
│   ├── test_analysis/           # Forecaster, calibration tests
│   ├── test_execution/          # Order building, risk check tests
│   ├── test_risk/               # Kelly sizer, circuit breaker tests
│   └── test_integration/        # End-to-end paper trading tests
├── scripts/
│   ├── setup_wallet.py          # Interactive wallet setup helper
│   ├── backfill_markets.py      # Historical market data loader
│   ├── discover_whales.py       # One-time whale basket discovery
│   └── run_backtest.py          # Backtest strategies on historical data
├── data/
│   ├── markets.db               # SQLite database
│   ├── chroma/                  # ChromaDB vector store
│   └── logs/                    # Structured log files
├── requirements.txt
├── pyproject.toml
├── Makefile                     # Common commands (test, lint, run, paper-trade, live)
└── README.md
```

### Technology Stack
| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| Runtime | Python | 3.12+ | Core platform |
| Polymarket SDK | py-clob-client | 0.34.6 | Trading API |
| AI Engine | anthropic | latest | Claude API calls |
| Web Framework | FastAPI | latest | Dashboard + alert API |
| Database | SQLite + SQLAlchemy | — | Trade log, calibration data |
| Vector DB | ChromaDB | latest | Market similarity search |
| Embeddings | sentence-transformers (e5-large-v2) | — | Market vectorization |
| HTTP Client | httpx | latest | Gamma API, Data API, news feeds |
| WebSocket | websockets | latest | Real-time orderbook |
| Scheduling | APScheduler | latest | Periodic scanning cycles |
| Data | pandas, numpy | latest | Analysis, backtesting |
| Config | pydantic-settings | latest | Typed configuration |
| Process Manager | pm2 (on Mac Mini) | latest | Keep bot running 24/7 |
| Testing | pytest, pytest-asyncio | latest | Test suite |

### Dependencies to Install
```bash
pip install py-clob-client anthropic httpx websockets fastapi uvicorn \
    sqlalchemy chromadb sentence-transformers apscheduler pandas numpy \
    pydantic-settings python-dotenv jinja2 pytest pytest-asyncio aiohttp \
    feedparser
```

---

## 4. PHASE 0: Infrastructure & Wallet Setup
**Duration: 1–2 hours | No code — setup only**
**Goal: Have a funded Polymarket wallet with API access confirmed**

### Tasks
- [ ] Install MetaMask browser extension or use existing wallet
- [ ] Fund wallet with USDC on Polygon network (start with $50–100 for testing)
- [ ] Export private key and store securely (NEVER in code, NEVER in git)
- [ ] Create `.env` file with: `PRIVATE_KEY`, `FUNDER_ADDRESS`, `ANTHROPIC_API_KEY`
- [ ] Test read-only API access (no auth needed): `GET https://gamma-api.polymarket.com/markets?limit=5`
- [ ] Test py-clob-client Level 0 connection: `client.get_ok()` and `client.get_server_time()`
- [ ] Derive API credentials: `client.create_or_derive_api_creds()`
- [ ] Test Level 2 access: `client.get_balance_allowance()`
- [ ] If using EOA wallet, set token allowances (one-time): approve USDC + conditional tokens for exchange contracts
- [ ] Place a $1 test trade manually via SDK to confirm full pipeline works
- [ ] Create git repo, add `.gitignore` (exclude `.env`, `data/`, `__pycache__/`, `*.db`)

### Exit Criteria
- Can read market data from Gamma API
- Can authenticate and see balance via CLOB API
- Have successfully placed and received fill on a $1 test trade
- `.env` file created and excluded from git

---

## 5. PHASE 1: Core Foundation & Market Scanner
**Duration: 1–2 days | ~800–1200 lines of code**
**Goal: Scan all active markets, filter by criteria, store in database**

### What to Build

#### 5.1 `config/settings.yaml`
```yaml
polymarket:
  clob_host: "https://clob.polymarket.com"
  gamma_host: "https://gamma-api.polymarket.com"
  data_host: "https://data-api.polymarket.com"
  chain_id: 137
  signature_type: 1  # 0 for EOA, 1 for proxy/email wallet

scanning:
  interval_seconds: 300  # 5 minutes
  min_volume_24h: 10000  # $10K minimum daily volume
  min_liquidity: 5000    # $5K minimum order book depth
  max_markets: 200       # Max markets to track simultaneously
  target_categories:
    - "Politics"
    - "Geopolitics"
    - "Fed"
    - "AI"
    - "Tech"
    - "Culture"
    - "Earnings"
  exclude_categories:
    - "Crypto Prices"  # Fee-enabled, HFT dominated
    - "Sports"          # Fees being rolled out

trading:
  mode: "paper"  # "paper" or "live"
  bankroll: 500.0
  max_position_pct: 0.05        # 5% of bankroll per position
  max_total_exposure_pct: 0.40  # 40% total
  max_correlated_exposure_pct: 0.20  # 20% in related markets
  min_edge_ai: 0.05             # 5% minimum edge for AI strategy
  min_edge_arb: 0.02            # 2% minimum edge for arbitrage
  kelly_fraction: 0.5           # Half-Kelly
  prefer_maker: true            # Always use limit orders when possible
  daily_loss_limit_pct: 0.10    # 10% daily loss → circuit breaker

claude:
  model_primary: "claude-sonnet-4-6"     # Fast, for routine assessments
  model_highstakes: "claude-opus-4-6"  # Thorough, for large positions
  highstakes_threshold: 50.0    # Use Opus for positions > $50
  max_tokens: 2000
  temperature: 0.3              # Lower = more deterministic

alerts:
  enabled: true
  imessage_enabled: true        # Route through VAYU iMessage infra
  daily_report_time: "21:00"    # 9 PM PT daily summary

database:
  path: "data/markets.db"
  wal_mode: true
```

#### 5.2 `src/core/models.py` — Pydantic Data Models
Define strict typed models for:
- `Market`: id, question, description, category, tokens (YES/NO with token_ids), end_date, volume_24h, liquidity, current_prices, resolution_source, active
- `MarketSnapshot`: market_id, timestamp, yes_price, no_price, spread, volume_1h
- `Signal`: strategy_name, market_id, direction (BUY_YES/BUY_NO), edge, confidence, reasoning, timestamp
- `Order`: id, market_id, token_id, side, price, size, order_type, status, created_at, filled_at
- `Position`: market_id, token_id, side, size, avg_entry_price, current_price, unrealized_pnl
- `Trade`: order_id, market_id, execution_price, size, fee, realized_pnl, timestamp
- `CalibrationRecord`: prediction_id, market_id, predicted_probability, market_price_at_prediction, actual_outcome, resolved_at

#### 5.3 `src/core/gamma_client.py` — Market Discovery
- `get_active_markets(limit, offset, tag, active=True)` → list of raw market dicts
- `get_market_by_id(condition_id)` → single market with full detail
- `get_events(limit, offset)` → events (which contain multiple markets)
- Pagination handling (Gamma API returns paginated results)
- Rate limit handling with exponential backoff
- Response parsing into `Market` Pydantic models

#### 5.4 `src/core/polymarket_client.py` — Authenticated Wrapper
- Initialize ClobClient with credentials from `.env`
- `get_price(token_id, side)` → current best price
- `get_midpoint(token_id)` → midpoint price
- `get_order_book(token_id)` → full order book with bids/asks
- `get_balance()` → USDC balance
- `get_positions()` → all open positions
- Retry logic with exponential backoff on 429/5xx
- Connection health check (`get_ok()`)

#### 5.5 `src/data/market_scanner.py` — Market Scanning Engine
- `scan_all_markets()` → Fetches all active markets from Gamma API
- `filter_markets(markets)` → Applies volume, liquidity, category filters from config
- `rank_markets(markets)` → Score by: (volume × spread × days_to_resolution_score)
- `update_database(markets)` → Upsert market data and snapshots into SQLite
- `get_top_opportunities(n=50)` → Return top N markets by opportunity score
- Runs on APScheduler every `scanning.interval_seconds`

#### 5.6 `src/storage/database.py` — SQLite Setup
- Create tables: markets, market_snapshots, signals, orders, positions, trades, calibration_records, whale_wallets, whale_trades
- WAL mode enabled for concurrent access
- Migration support (simple version-based)
- Helper methods: `upsert_market()`, `log_signal()`, `log_trade()`, `get_active_positions()`

### Tests for Phase 1
- `test_gamma_client.py`: Mock API responses, test pagination, test filtering
- `test_polymarket_client.py`: Mock ClobClient, test price fetching, error handling
- `test_market_scanner.py`: Test filtering logic, ranking algorithm, database upserts
- `test_models.py`: Pydantic validation, edge cases (missing fields, bad types)
- `test_database.py`: Table creation, upsert, query correctness

### Exit Criteria
- Market scanner runs, fetches 100+ active markets from Gamma API
- Markets are filtered to ~30–60 qualifying markets
- Market data is stored in SQLite with snapshots
- All Phase 1 tests pass
- Can run: `python -m src.main` and see scanner output in logs

---

## 6. PHASE 2: AI Probability Engine
**Duration: 2–3 days | ~1000–1500 lines of code**
**Goal: Claude analyzes markets and produces calibrated probability estimates**

### What to Build

#### 6.1 `src/analysis/prompt_templates.py`
Structured prompt templates for each market category. Each template includes:
- Role definition: "You are a calibrated probability forecaster..."
- Market context: question, resolution criteria, current market price
- Required inputs: recent news context, historical base rates, expert opinions
- Output format: JSON with fields: `probability`, `confidence_low`, `confidence_high`, `key_factors_for`, `key_factors_against`, `uncertainties`, `reasoning`
- Calibration instruction: "If you estimate 70%, that means in 100 similar situations, ~70 should resolve YES"

**Critical prompt design principles (from research):**
- ALWAYS include the current market price — this enables Claude to assess mispricing, not just predict in a vacuum
- ALWAYS request base rates — anchors the estimate in historical precedent
- ALWAYS include resolution criteria verbatim — many losses come from misunderstanding how a market resolves
- Request confidence intervals, not just point estimates
- Ask Claude to identify what would change its estimate (key uncertainties)

Template categories:
- `POLITICS_TEMPLATE` — election markets, policy decisions, nominations
- `FED_MACRO_TEMPLATE` — interest rates, CPI, employment
- `GEOPOLITICS_TEMPLATE` — international events, conflicts, treaties
- `TECH_AI_TEMPLATE` — product launches, AI benchmarks, regulatory
- `CULTURE_TEMPLATE` — awards, entertainment, viral events
- `GENERAL_TEMPLATE` — fallback for uncategorized markets

#### 6.2 `src/analysis/claude_forecaster.py`
- `assess_market(market: Market, news_context: str = "") -> ForecastResult`
  - Selects appropriate prompt template based on market category
  - Constructs full prompt with market data + any gathered context
  - Calls Claude API (sonnet for routine, opus for high-stakes)
  - Parses JSON response into `ForecastResult` model
  - Handles API errors, rate limits, malformed responses
  - Logs raw prompt + response for calibration review
- `ForecastResult` model: probability, confidence_interval, key_factors, uncertainties, reasoning, model_used, tokens_used, latency_ms

#### 6.3 `src/analysis/ensemble.py`
- `ensemble_forecast(market, forecasts: list[ForecastResult]) -> EnsembleForecast`
- Phase 2: Simple average of Claude's estimate + market price (extremal adjustment)
- Phase 8 upgrade: Add second model, Brier-score-weighted average
- Extremal adjustment: If Claude says 70% and market says 50%, the ensemble might output 62% (weighted toward Claude but pulled toward market for humility)

#### 6.4 `src/analysis/market_classifier.py`
- `classify_market(market: Market) -> str` — Returns category string
- Uses keyword matching on market question + tags from Gamma API
- Maps to prompt template selection
- Also classifies: `is_binary`, `has_clear_resolution`, `days_to_resolution`, `liquidity_tier`

#### 6.5 `src/strategies/ai_probability.py`
- `scan_for_opportunities(markets: list[Market]) -> list[Signal]`
  - For each qualifying market:
    1. Classify market category
    2. Get current price from CLOB API
    3. Run Claude forecast
    4. Calculate edge: `claude_probability - market_price` (for BUY_YES) or `(1 - claude_probability) - (1 - market_price)` (for BUY_NO)
    5. If `abs(edge) >= min_edge_ai`: generate Signal
    6. Signal includes: direction, edge size, confidence, reasoning
  - Rate-limit-aware: don't burn all Claude API budget in one scan
  - Priority: assess highest-volume markets first

#### 6.6 `src/strategies/obvious_no.py` — Low-Risk Base Yield
- Scan for markets where YES is trading at $0.01–$0.05 on absurd outcomes
- "Will aliens make contact in 2026?" YES at $0.03 → buy NO at $0.97 → 3% return at resolution
- Filters: must have clear resolution date within 30 days, minimum $5K volume, no ambiguous resolution criteria
- Calculate annualized return: `(1 - no_price) / no_price * (365 / days_to_resolution)`
- Only enter if annualized return > 20% AND resolution is unambiguous
- Risk: black swan events can crash near-certain markets. Max exposure: 10% of bankroll in obvious-no positions.

### Tests for Phase 2
- `test_prompt_templates.py`: Verify template construction, all required fields present
- `test_claude_forecaster.py`: Mock Claude API, test response parsing, error handling, malformed JSON
- `test_market_classifier.py`: Test category assignment accuracy across sample markets
- `test_ai_probability.py`: Test edge calculation, signal generation, min-edge filtering
- `test_obvious_no.py`: Test annualized return calculation, filter logic

### Exit Criteria
- Claude can assess any market and return a structured probability estimate
- Edge calculator correctly identifies mispriced markets
- Obvious NO scanner finds qualifying low-risk opportunities
- All Phase 2 tests pass
- Manual spot-check: Claude's estimates are reasonable for 10 randomly selected markets

---

## 7. PHASE 3: Paper Trading & Calibration
**Duration: 2–3 days building + 2 weeks running | ~800–1000 lines of code**
**Goal: Simulate trades without real money, track accuracy, build calibration baseline**

### What to Build

#### 7.1 `src/execution/order_builder.py`
- `build_limit_order(market, token_id, side, price, size) -> SignedOrder`
- `build_market_order(market, token_id, side, amount) -> SignedOrder`
- Validates: sufficient balance, within risk limits, price sanity checks
- Sets `feeRateBps` correctly (0 for event markets, dynamic for fee-enabled)
- Uses maker (GTC limit) orders by default

#### 7.2 `src/execution/order_router.py`
- `route_order(order, mode="paper"|"live") -> OrderResult`
- Paper mode: Simulates fill at specified price, logs to database, updates paper portfolio
- Live mode: Submits to CLOB API via py-clob-client, monitors for fill
- Both modes go through identical risk checks first

#### 7.3 `src/execution/position_manager.py`
- `get_all_positions() -> list[Position]`
- `update_positions()` — Refresh prices, recalculate unrealized P&L
- `get_total_exposure() -> float`
- `get_correlated_exposure(market_id) -> float` — Uses market graph to find related positions
- `should_exit(position) -> (bool, reason)` — Time-based exit, edge-gone exit, stop-loss

#### 7.4 `src/risk/risk_engine.py` — Central Risk Gate
Every trade must pass ALL checks:
1. `check_balance(size)` — Sufficient USDC
2. `check_position_limit(size)` — Under 5% of bankroll
3. `check_total_exposure(size)` — Under 40% total
4. `check_correlated_exposure(market_id, size)` — Under 20% correlated
5. `check_daily_loss_limit()` — Circuit breaker not triggered
6. `check_market_liquidity(market, size)` — Order book can absorb without >2% slippage
7. `check_existing_position(market_id)` — Don't double-enter same market
8. `check_edge_minimum(edge, strategy)` — Edge exceeds threshold for strategy type
9. `check_resolution_date(market)` — Market resolves within reasonable timeframe
10. `check_cooldown(market_id)` — Not recently exited this market
Returns: `RiskCheckResult(passed: bool, failed_checks: list[str], warnings: list[str])`

#### 7.5 `src/risk/kelly_sizer.py`
- `calculate_position_size(edge, probability, bankroll, current_exposure) -> float`
- Kelly formula: `f = (p * b - q) / b` where p=probability, b=odds, q=1-p
- Apply half-Kelly: `f = f * kelly_fraction`
- Apply caps: min(f * bankroll, bankroll * max_position_pct)
- Reduce if near total exposure limit

#### 7.6 `src/risk/circuit_breaker.py`
- Tracks daily realized + unrealized P&L
- If daily loss exceeds `daily_loss_limit_pct`: halt ALL trading for rest of day
- If 3 consecutive losing days: reduce position sizing to quarter-Kelly
- If 5 consecutive losing days: halt trading, send alert to review strategy
- Manual override via config to resume

#### 7.7 `src/analysis/calibration.py`
- `log_prediction(market_id, predicted_prob, market_price)`
- `resolve_prediction(market_id, actual_outcome)`
- `calculate_brier_score(period="all"|"30d"|"7d") -> float` — Lower = better calibrated
- `get_calibration_chart_data()` → Bins predictions (0-10%, 10-20%, etc.) and actual resolution rates
- `get_accuracy_by_category()` → Which categories are we best/worst at
- `get_edge_vs_actual()` → When we saw 5% edge, what was actual return?
- Perfect calibration = Brier score 0.0, random guessing = 0.25

#### 7.8 `src/main.py` — Orchestrator (Paper Trading Mode)
```python
async def main():
    # Initialize all components
    config = load_config()
    db = Database(config.database.path)
    gamma = GammaClient(config)
    clob = PolymarketClient(config)
    scanner = MarketScanner(gamma, db, config)
    forecaster = ClaudeForecaster(config)
    risk = RiskEngine(config, db)
    sizer = KellySizer(config)
    router = OrderRouter(config, clob, db)
    position_mgr = PositionManager(clob, db)
    calibration = CalibrationTracker(db)

    # Strategy instances
    ai_strategy = AIProbabilityStrategy(forecaster, config)
    no_strategy = ObviousNoStrategy(config)

    # Main loop
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan_and_trade, 'interval', seconds=config.scanning.interval_seconds)
    scheduler.start()

    async def scan_and_trade():
        markets = await scanner.scan_all_markets()
        filtered = scanner.filter_markets(markets)

        # AI Probability signals
        ai_signals = await ai_strategy.scan_for_opportunities(filtered[:30])
        # Obvious NO signals
        no_signals = no_strategy.scan_for_opportunities(filtered)

        all_signals = ai_signals + no_signals
        for signal in sorted(all_signals, key=lambda s: abs(s.edge), reverse=True):
            risk_result = risk.check_all(signal)
            if risk_result.passed:
                size = sizer.calculate_position_size(signal.edge, signal.probability, ...)
                order = order_builder.build_limit_order(...)
                result = await router.route_order(order, mode=config.trading.mode)
                calibration.log_prediction(signal.market_id, signal.probability, signal.market_price)
```

### Tests for Phase 3
- `test_risk_engine.py`: Test all 10 checks individually and in combination
- `test_kelly_sizer.py`: Test sizing across edge/probability combinations, cap enforcement
- `test_circuit_breaker.py`: Test daily loss trigger, consecutive loss detection
- `test_order_router.py`: Test paper mode fill simulation, database logging
- `test_calibration.py`: Test Brier score calculation, binning, category breakdown
- `test_integration_paper.py`: End-to-end paper trade cycle

### Running Paper Trading
```bash
# Start paper trading
python -m src.main

# Let it run for 2 weeks minimum
# Check calibration daily:
python -m scripts.check_calibration

# Review performance:
python -m scripts.paper_trade_report
```

### Exit Criteria for Phase 3
- Paper trading system runs continuously for 2+ weeks without crashes
- At least 50 paper trades executed across AI probability + obvious NO strategies
- Calibration data shows Brier score < 0.20 (reasonable calibration)
- Win rate between 55–70% on AI probability signals
- Risk engine correctly blocks over-sized positions
- Circuit breaker triggers correctly on simulated loss scenarios
- All Phase 3 tests pass

### DECISION GATE: Do NOT proceed to Phase 4 (live trading) unless:
1. Paper trading shows positive expected value over 50+ trades
2. Brier score is reasonable (< 0.20)
3. No critical bugs discovered during paper trading period
4. You are comfortable with the sizing and risk parameters

---

## 8. PHASE 4: Live Trading & Risk Engine
**Duration: 1–2 days coding + ongoing operation | ~300–500 lines**
**Goal: Switch from paper to live execution with enhanced safety**

### What to Build

#### 8.1 Live Order Execution in `order_router.py`
- Live mode submits orders to CLOB API via `client.create_order()` + `client.post_order()`
- Monitor fill status via `client.get_order()` or WebSocket user channel
- Handle partial fills, order expiry, rejection
- Implement cancel-and-replace for stale orders

#### 8.2 `src/core/websocket_client.py` — Real-Time Feed
- Connect to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe to price updates for all tracked markets
- Subscribe to user channel for order fill notifications
- Auto-reconnect on disconnect
- Feed real-time prices to position manager

#### 8.3 Enhanced `position_manager.py`
- Sync with on-chain positions via `client.get_positions()`
- Reconcile paper positions with actual positions
- Track slippage: expected fill price vs actual
- Auto-exit logic for time-based and edge-gone conditions

#### 8.4 Three-Gate Safety System (inspired by the fully-autonomous bot)
- **Gate 1**: Config file must have `trading.mode: "live"` (not default)
- **Gate 2**: Environment variable `POLYEDGE_LIVE_ENABLED=true` must be set
- **Gate 3**: On first live trade of each session, require manual confirmation via console input
- All three must be unlocked for live orders to execute

### Starting Live
```bash
# Set env var
export POLYEDGE_LIVE_ENABLED=true

# Update config
# settings.yaml: trading.mode: "live"

# Start with small bankroll ($200-500)
python -m src.main

# First trade will prompt: "LIVE TRADE: BUY NO on 'Will X...' at $0.72, size $15.00. Confirm? [y/N]"
```

### Exit Criteria
- Successfully execute 5+ live trades with correct fill prices
- Position tracking matches on-chain reality
- WebSocket reconnects reliably after disconnection
- Three-gate safety system prevents accidental live trades
- Daily P&L reporting is accurate

---

## 9. PHASE 5: Cross-Market Arbitrage Scanner
**Duration: 3–5 days | ~1500–2000 lines**
**Goal: Detect and exploit logical pricing inconsistencies between related markets**

### What to Build

#### 9.1 `src/data/market_graph.py` — Vector Store for Market Relationships
- Index all active markets into ChromaDB using e5-large-v2 embeddings
- Store: market question text, category, end_date, token_ids
- `find_related_markets(market_id, n=10)` — Semantic similarity search
- `build_relationship_graph()` — For each market, identify: subsets, supersets, mutual exclusives
- Update graph on each scanner cycle

#### 9.2 `src/strategies/cross_arb.py` — Arbitrage Detection Engine
Three arbitrage types:

**Type A: Intra-Market Rebalancing**
- For each market, check: `yes_price + no_price < 1.00`
- If sum < 0.98 (2% minimum edge after fees): signal
- Simple and fast, but opportunities last ~2.7 seconds

**Type B: Logical/Subset Arbitrage**
- Use Claude to validate logical relationships between related markets
- Example: If "Trump wins" = 35% and "Republican wins" = 32%, that's impossible
- Buy the underpriced superset, sell the overpriced subset
- These persist MUCH longer — minutes to hours — because they require understanding relationships

**Type C: Mutual Exclusivity Sum Check**
- For multi-outcome events (e.g., "Who wins the primary?" with 5 candidates)
- Sum of all outcome prices should = 100%
- If sum > 102%: sell the overpriced outcomes
- If sum < 98%: buy all outcomes for guaranteed profit

#### 9.3 Claude Validation Layer
- Before executing any cross-market arb, send both markets to Claude
- "Do these two markets have a logical subset/superset relationship? If Market A resolves YES, must Market B also resolve YES?"
- This prevents false positives from markets with subtly different resolution criteria
- Cache validated relationships to avoid repeated API calls

### Tests for Phase 5
- `test_market_graph.py`: Test embedding, similarity search, relationship detection
- `test_cross_arb.py`: Test all three arbitrage type detectors with known examples
- `test_arb_validation.py`: Test Claude validation with tricky edge cases

### Exit Criteria
- Scanner identifies 1-5+ logical arbitrage opportunities per day
- Claude validation correctly filters false positives
- Paper trade arb opportunities for 1 week before going live
- Arbitrage P&L is positive on paper trades

---

## 10. PHASE 6: Whale Signal Tracking
**Duration: 3–4 days | ~1200–1500 lines**
**Goal: Monitor proven wallets and trade on consensus signals**

### What to Build

#### 10.1 `scripts/discover_whales.py` — One-Time Whale Discovery
- Scrape Polymarket leaderboard (polymarket.com/leaderboard/overall/monthly/profit)
- Filter for: 50+ trades, 55%+ win rate, positive P&L last 90 days, focuses on event markets
- Verify wallets aren't correlated (checking similar trade patterns)
- Output: `config/whale_basket.yaml` with 5-10 wallet addresses

#### 10.2 `src/data/whale_monitor.py`
- Poll Data API for each whale's positions and recent trades
- `get_whale_positions(wallet_address) -> list[Position]`
- `get_whale_recent_trades(wallet_address, hours=24) -> list[Trade]`
- Detect new position entries (position didn't exist in last check, now it does)
- Rate-limit aware: stagger API calls across wallets

#### 10.3 `src/strategies/whale_tracker.py`
- `check_consensus(market_id) -> ConsensusSignal`
- Consensus rule: Signal generated when 80%+ of basket agrees (e.g., 4/5 or 8/10 whales buy YES)
- Timing weight: Early entries (>24h before major news) get higher confidence than late entries
- Size calibration: Position size proportional to whale's allocation percentage
- Dedup: Don't re-enter a market where whales have been in for days (you'd be late)

### Exit Criteria
- 5-10 whale wallets identified and monitored
- Consensus engine correctly detects agreement patterns
- Paper trade whale signals for 1 week
- Whale signal win rate on paper is > 55%

---

## 11. PHASE 7: News-Reactive Trading
**Duration: 3–5 days | ~1500–2000 lines**
**Goal: Detect breaking news, assess impact on markets, trade before repricing**

### What to Build

#### 11.1 `src/data/news_ingestion.py`
- RSS feeds: Reuters, AP, Bloomberg, major political news
- Optional: Twitter/X API for real-time (if budget allows $100/mo)
- Google News API or scraping for topic-specific alerts
- `get_recent_news(topic, hours=1) -> list[NewsItem]`
- Relevance scoring: keyword match against active market questions

#### 11.2 `src/strategies/news_reactive.py`
- When new high-relevance news detected:
  1. Map to affected markets (keyword + semantic matching)
  2. Send to Claude for rapid impact assessment (use sonnet for speed)
  3. Claude outputs: affected market, direction, probability shift estimate, confidence
  4. If shift > min_edge: generate signal
- Speed target: news-to-signal in under 30 seconds
- Markets often take 2-8 minutes to fully reprice, so even 60s is plenty of edge

### Exit Criteria
- News pipeline detects relevant stories within 5 minutes of publication
- Claude impact assessment completes in under 15 seconds
- Paper trade news signals for 2 weeks
- News signal accuracy is > 60%

---

## 12. PHASE 8: Dashboard, Alerts & Optimization
**Duration: 3–5 days | ~2000–2500 lines**
**Goal: Full visibility, iMessage alerts, performance optimization**

### What to Build

#### 12.1 `src/dashboard/server.py` — FastAPI Web Dashboard
- Portfolio overview: bankroll, total P&L, open positions, exposure
- Strategy breakdown: per-strategy P&L, win rate, Brier score
- Calibration chart: predicted probability vs actual resolution rate
- Recent signals: what was detected, what was traded, what was skipped (and why)
- Whale activity feed: what tracked wallets are doing
- Market scanner results: current top opportunities

#### 12.2 `src/alerts/imessage_alert.py`
- Bridge to existing VAYU iMessage infrastructure via HTTP API
- Alert types: new signal detected, trade executed, daily P&L summary, circuit breaker triggered
- One-click trade URLs in alerts for semi-auto mode

#### 12.3 `src/alerts/daily_report.py`
- End-of-day summary: trades today, P&L today, total P&L, win rate, biggest win/loss
- Calibration update: rolling 7-day Brier score
- Strategy performance comparison
- Whale basket performance
- Sent via iMessage at configured time

#### 12.4 Optimization & Calibration Refinement
- Adaptive model weighting based on Brier scores per category
- If Claude is great at politics but bad at entertainment, increase/decrease trust accordingly
- Track edge decay: do detected edges actually result in profits?
- A/B test different prompt templates

### Exit Criteria
- Dashboard accessible at localhost:8080
- iMessage alerts working for trade notifications
- Daily report generated and sent automatically
- Calibration loop shows measurable improvement over time

---

## 13. TESTING STRATEGY

### Test Pyramid
- **Unit tests** (60%): Individual functions, edge calculation, filtering logic, model validation
- **Integration tests** (25%): API client → scanner → forecaster → signal generation
- **End-to-end tests** (15%): Full paper trade cycle from scan to execution

### Test Commands
```bash
# Run all tests
make test

# Run specific phase
pytest tests/test_core/ -v
pytest tests/test_strategies/ -v

# Run with coverage
pytest --cov=src --cov-report=html

# Run integration tests (requires API access)
pytest tests/test_integration/ -v --run-integration
```

### Target: 80%+ coverage, all tests pass before each phase goes live

---

## 14. KEY TECHNICAL REFERENCES

### Polymarket APIs
- Gamma API (markets): `https://gamma-api.polymarket.com` — no auth, read-only
- CLOB API (trading): `https://clob.polymarket.com` — L1/L2 auth for trading
- Data API (positions): `https://data-api.polymarket.com` — no auth
- WebSocket: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Leaderboard: `https://polymarket.com/leaderboard/overall/monthly/profit`

### SDKs
- Python: `pip install py-clob-client` (v0.34.6, MIT license)
- Docs: `https://docs.polymarket.com`
- GitHub: `https://github.com/Polymarket/py-clob-client`

### Open Source Reference Implementations
- Official agents framework: `https://github.com/Polymarket/agents` (MIT)
- Fully autonomous bot: `https://github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot`
- Cross-platform arb: `https://github.com/ImMike/polymarket-arbitrage`
- Academic paper (86M trade analysis): `https://arxiv.org/abs/2508.03474`

### Anthropic
- Python SDK: `pip install anthropic`
- API docs: `https://docs.anthropic.com`

---

## 15. KNOWN PITFALLS & HOW TO AVOID THEM

### Pitfall 1: Treating Polymarket Like a Sportsbook
**Problem**: Buying YES at $0.66 because you think it's "likely." This creates a worst-case payoff structure: 50% upside, 100% downside.
**Solution**: Only trade when you have a QUANTIFIED edge (your estimated probability > market price). The edge must exceed minimum threshold after accounting for uncertainty.

### Pitfall 2: Asymmetric Sizing
**Problem**: A trader won 51% of trades but lost $2M because losses were 3-10x larger than wins.
**Solution**: Half-Kelly sizing ensures position size is proportional to edge size. Hard cap at 5% per position. Circuit breaker on daily losses.

### Pitfall 3: Resolution Criteria Mismatch
**Problem**: You bet on "Will X happen?" but the resolution criteria has a specific technical definition that differs from the common understanding.
**Solution**: Always include resolution criteria verbatim in Claude's assessment prompt. Claude validates the logical match before cross-market arbitrage.

### Pitfall 4: Overconfidence in AI Probability
**Problem**: Claude says 70%, you bet the farm. Claude was wrong. Account blown.
**Solution**: Half-Kelly. Max 5% per position. Calibration tracking. If Brier score degrades, reduce sizing automatically. Never trust a single model — ensemble check.

### Pitfall 5: Liquidity Illusion
**Problem**: Order book shows good price but can't fill meaningful size without slippage.
**Solution**: Risk engine checks order book depth before every trade. Max order size = 10% of best bid/ask depth. Prefer maker orders (no slippage by definition).

### Pitfall 6: Black Swan on "Obvious NO"
**Problem**: "Will aliens make contact?" was at 97% NO. Surprise announcement drops it to 50%. You lose your "safe" money.
**Solution**: Max 10% of bankroll in obvious-no positions. Only markets with truly unambiguous resolution. Diversify across many different obvious-no markets.

### Pitfall 7: API Rate Limits
**Problem**: Scanning too aggressively, hitting 429 errors, missing opportunities.
**Solution**: Exponential backoff with jitter. Stagger API calls. Cache aggressively. Use WebSocket for real-time data instead of polling.

### Pitfall 8: Key Security
**Problem**: Private key leaked → wallet drained.
**Solution**: `.env` file excluded from git. Dedicated trading wallet with limited funds. Never store more in the hot wallet than you're willing to lose. Consider hardware wallet for larger balances.

---

## PHASE DEPENDENCY MAP

```
Phase 0 (Wallet Setup)
    └── Phase 1 (Core + Scanner) ← FOUNDATION
            └── Phase 2 (AI Probability Engine)
                    └── Phase 3 (Paper Trading + Calibration) ← DECISION GATE
                            └── Phase 4 (Live Trading)
                                    ├── Phase 5 (Cross-Market Arb) ← Can run in parallel with 6/7
                                    ├── Phase 6 (Whale Tracking) ← Can run in parallel with 5/7
                                    ├── Phase 7 (News Reactive) ← Can run in parallel with 5/6
                                    └── Phase 8 (Dashboard + Alerts + Optimization)
```

**Total estimated development time: 4–8 weeks (depending on paper trading duration)**
**First live trades possible: End of Phase 4 (~2–3 weeks)**
**Full system operational: End of Phase 8 (~6–8 weeks)**

---

## CLAUDE CODE INSTRUCTIONS

When building each phase with Claude Code:
1. Read this CLAUDE.md first for full context
2. Follow the directory structure exactly
3. Use Pydantic models for ALL data structures
4. Write tests BEFORE or alongside implementation (not after)
5. Use `async/await` throughout — this is an async system
6. Log everything at DEBUG level during development, INFO in production
7. Never hardcode secrets — always read from environment variables
8. Run `pytest` and `mypy` after each module
9. Commit after each working module with descriptive message

The goal is a system that's reliable enough to run 24/7 on the Mac Mini alongside VAYU Order Sync, generating returns while you sleep.
