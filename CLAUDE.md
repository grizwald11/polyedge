# PolyEdge — AI-Driven Prediction Market Trading Bot

**Author:** Adam (VAYU / IO Distributors LLC) | **Created:** March 12, 2026 | **Platform:** Python 3.12+ on Mac Mini M4 Pro

## 1. PROJECT OVERVIEW

PolyEdge runs 7+ complementary strategies simultaneously on Kalshi (CFTC-regulated):
1. **AI Probability** — Claude estimates true probabilities, trades mispriced markets
2. **Cross-Market Logical Arb** — Pricing inconsistencies between related markets (same platform)
3. **Cross-Platform Arb** — Kalshi vs Polymarket discrepancies (min similarity 0.55)
4. **Whale Tracking** — Monitors proven wallets, trades on 80%+ basket consensus
5. **News-Reactive** — Breaking news → Claude impact assessment → trade before repricing
6. **Late Resolution** — Markets resolving <6h where outcome >90% certain but priced <80%
7. **Mean Reversion** — Fades >10% moves in 2h, max 2% bankroll, auto-closes in 4h

> **Note:** Polymarket is used as a read-only cross-reference for price validation only. All trade execution happens on Kalshi.

**Why it wins:** Fee-free event markets only | information edge (not speed) | multi-strategy diversification | paper-trade-first discipline | self-calibrating

**Revenue target:** Conservative 5–10% monthly, aggressive 15–30%. Top 1% prediction market traders have 55–67% win rates — edge comes from SIZING, not accuracy.

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

## 4. IMPLEMENTATION PHASES (Summary)

All phases are built. See git history for implementation details.

- **Phase 0:** Wallet setup, `.env` config, API auth verified
- **Phase 1:** Core models, Gamma/CLOB clients, market scanner, SQLite+WAL storage
- **Phase 2:** Claude forecaster (sonnet/opus), prompt templates per category, ensemble, market classifier, AI probability + obvious-no strategies
- **Phase 3:** Paper trading, order builder/router, position manager, risk engine (10 checks), half-Kelly sizer, circuit breaker, calibration tracking, main orchestrator loop
- **Phase 4:** Live execution, WebSocket price feeds, three-gate safety (config + env + manual confirm)
- **Phase 5:** ChromaDB market graph, cross-market arb (intra-market, logical/subset, mutual exclusivity), Claude relationship validation
- **Phase 6:** Whale discovery, monitoring, 80%+ basket consensus signals
- **Phase 7:** RSS news ingestion, news-reactive strategy (<30s news-to-signal)
- **Phase 8:** FastAPI dashboard, iMessage alerts, daily reports, adaptive optimization

### Key Design Rules (from phase specs)
- **Prompt rules:** ALWAYS include market price, base rates, resolution criteria verbatim, confidence intervals
- **Risk engine checks (ALL must pass):** balance, position limit (5%), total exposure (40%), correlated exposure (20%), daily loss limit, liquidity (<2% slippage), no double-entry, edge minimum, resolution date, cooldown
- **Kelly sizing:** `f = (p*b - q) / b`, half-Kelly, cap at 5% bankroll per position
- **Circuit breaker:** daily loss > 10% → halt; 3 losing days → quarter-Kelly; 5 → halt + alert
- **Cross-arb types:** A (intra-market yes+no<0.98), B (logical/subset, Claude-validated), C (mutual exclusivity sum≠100%)
- **Live trading gate:** Positive EV over 50+ paper trades, Brier <0.20, no critical bugs

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
