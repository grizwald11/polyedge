# PolyEdge Full Codebase Audit — 2026-03-28

Complete line-by-line audit of all 56 source files (~42K lines).
Issues marked **[FIXED]** were resolved in this commit.

---

## CRITICAL — Direct Money Loss or Crashes

### Models & Validation
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 1 | models.py:288 | Order.price validator allowed $0.00 → division by zero in Kelly | **[FIXED]** |
| 2 | models.py:228 | Signal.edge had no validator — NaN/infinity propagated to Kelly sizer | **[FIXED]** |
| 3 | models.py:423 | EnsembleForecast.edge had no validator — NaN/infinity possible | **[FIXED]** |
| 4 | models.py:246 | Signal.market_price allowed 0.0 and 1.0 → Kelly division by zero | **[FIXED]** |
| 5 | models.py:411 | CI inversion silently corrected without logging | **[FIXED]** |

### Ensemble & Forecasting
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 6 | ensemble.py:231 | Brier-weighted ensemble breaks when all scores=1.0 (weights don't normalize) | **[FIXED]** |
| 7 | ensemble.py:53 | CI width penalty uses abs() which masks inverted bounds → confidence inflated | Open |

### Data Layer
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 8 | whale_monitor.py:148 | Division by zero when whale basket is empty | **[FIXED]** |
| 9 | news_ingestion.py:111 | Division by zero when market question produces empty word set | Open |

### Strategies
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 10 | obvious_no.py:86 | Edge could be zero or negative without guard | **[FIXED]** |
| 11 | cross_arb.py:80 | Truthiness check `not market.yes_price` treats $0.00 as falsy | Open |

---

## HIGH — Logic Errors, Security, Silent Failures

### API Clients
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 12 | kalshi_client.py:138 | Rate limit (429) returns None silently instead of raising | Open |
| 13 | websocket_client.py:356 | WebSocket auth failure returns empty dict silently — fills never received | Open |
| 14 | polymarket_client.py:102 | Balance conversion heuristic (`/1e6 if >1000`) is fragile | Open |
| 15 | polymarket_client.py:113 | Price parsing can crash on malformed API response (no try/except) | Open |
| 16 | kalshi_client.py:286 | get_balance() doesn't validate non-negative balance | Open |

### Security
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 17 | prompt_templates.py:230 | Prompt injection risk: market question/description injected unsanitized | Open |
| 18 | claude_forecaster.py:102 | Prompt injection risk: news context from untrusted sources injected raw | Open |
| 19 | kalshi_client.py:52 | Private key file opened without checking file permissions (should be 600) | Open |

### Execution
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 20 | main.py:283 | Exit orders bypass risk engine entirely (no balance/circuit breaker check) | Open |
| 21 | order_router.py:206 | Three-gate live safety: Gate 3 bypassed permanently after first confirmation | Open |
| 22 | position_manager.py:262 | Take-profit uses `1.0 - avg_entry_price` which is wrong for BUY_NO positions | Open |

### Database
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 23 | database.py:791-826 | get_positions_with_pnl() CTE missing total_cost/total_fees → runtime crash | Open |
| 24 | database.py:245 | Foreign keys disabled globally, no app-level enforcement | Open |

### Backtest
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 25 | backtest_engine.py:80-120 | Lookahead bias: synthetic forecasts use known outcomes | Open |
| 26 | backtest_engine.py:257 | Survivorship bias: only settled markets included | Open |
| 27 | backtest_engine.py:372 | Fees not simulated in backtest | Open |

---

## MEDIUM — Edge Cases, Inefficiencies, Data Quality

### Risk & Sizing
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 28 | risk_engine.py:140 | Edge == probability_estimate allowed (implies market_price=0) | Open |
| 29 | risk_engine.py:41 | Cooldown persistence doesn't validate datetime format from DB | Open |
| 30 | kelly_sizer.py:34 | fee_rate hardcoded to 0.0 — correct for event markets but fragile | Open |

### Data Enrichment
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 31 | fedwatch.py:69 | FedWatch regex fragile — CME page redesign silently disables it | Open |
| 32 | cleveland_fed.py:63 | Cleveland Fed parsing fragile — site redesign silently disables it | Open |
| 33 | metaculus_client.py:107 | Metaculus disabled for entire session if first probe fails | Open |
| 34 | news_ingestion.py:60 | _seen_urls set grows unbounded → memory leak on long runs | Open |
| 35 | polymarket_cross_ref.py:67 | Price parsing falls back to stale bestBid when outcomePrices malformed | Open |

### Market Scanning
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 36 | market_discovery.py:191 | Unknown market status (e.g., "resolving") silently dropped | Open |
| 37 | polymarket_discovery.py:43 | Outcome prices not bounds-checked after parsing | Open |
| 38 | market_scanner.py:130 | log10(volume) crashes on volume=0 | Open |
| 39 | polymarket_scanner.py:89 | Price sum tolerance 0.90-1.10 too loose (10% deviation allowed) | Open |
| 40 | market_discovery.py:186 | Negative spread (bid > ask) not validated | Open |

### Calibration
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 41 | calibration.py:115 | Records with bad actual_outcome silently skipped (no logging) | Open |
| 42 | claude_forecaster.py:449 | Default CI (±0.15) creates false precision when Claude omits bounds | Open |
| 43 | calibration.py:224 | Win rate uses flat 0.5 threshold regardless of edge size | Open |

### Execution Details
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 44 | fill_tracker.py:254 | Order.size mutation during clamp breaks partial fill tracking | Open |
| 45 | main.py:212 | Zero-price fallback creates invalid Market with price=0 | Open |
| 46 | main.py:109 | Bankroll read once at startup, never re-synced to actual balance | Open |
| 47 | main.py:646 | Timeout error message hardcoded "(>5 minutes)" vs config value | Open |

### Configuration
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 48 | config.py:176 | Missing ANTHROPIC_API_KEY not caught until first Claude call | Open |
| 49 | config.py:181 | Missing Polymarket private key not caught until first PM order | Open |
| 50 | settings.yaml:69 | cross_check_disagreement_threshold (0.12) differs from config.py default (0.15) | Open |

### Async / Concurrency
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 51 | polymarket_client.py:75 | Uses deprecated asyncio.get_event_loop() | Open |
| 52 | websocket_client.py:243 | Price callbacks awaited sequentially — slow callback blocks feed | Open |
| 53 | websocket_client.py:159 | Reconnect loop spins forever on permanent auth failure | Open |

### Backtest Details
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 54 | backtest_engine.py:314+387 | Positions can be resolved twice (mid-loop + final loop) | Open |
| 55 | backtest_engine.py:322 | Division by zero: pnl / (size * price) when size=0 | Open |
| 56 | backfill_markets.py:198 | volume_1h is trade count, not dollar volume | Open |
| 57 | backfill_markets.py:262 | Synthetic snapshots use linear drift (unrealistic) | Open |

---

## LOW — Code Quality, Minor Edge Cases

| # | File:Line | Issue |
|---|-----------|-------|
| 58 | models.py:132 | MarketToken.price defaults to 0.0 (should require explicit value) |
| 59 | config.py:147 | Database path not validated for empty string |
| 60 | config.py:154 | Logging level not validated against valid Python levels |
| 61 | market_graph.py:45 | ChromaDB failure falls back to weak keyword matching silently |
| 62 | market_graph.py:193 | Keyword tokenization misses hyphenated terms |
| 63 | cache.py:27 | TTL uses time.monotonic() — edge case if system clock jumps |
| 64 | news_ingestion.py:34 | Timezone-naive published dates treated as UTC (may be EST) |
| 65 | news_researcher.py:337 | Context truncated by chars not tokens (~800 tokens approximation) |
| 66 | resolution_tracker.py:133 | Resolution inferred from yes_price>0.5 — ambiguous at exactly 0.5 |
| 67 | calibration_analyzer.py:150 | Base rate requires ≥8 samples (hard cutoff, no confidence interval) |
| 68 | portfolio_risk.py:38 | Unknown markets default to own cost_basis (conservative but redundant) |
| 69 | order_builder.py:161 | Price clamping is silent (no warning logged) |
| 70 | dashboard.py:194 | Daily loss calculation uses confusing min(0, pnl) pattern |
| 71 | daily_report.py:76 | Win rate calculation fragile (works but unclear) |
| 72 | database.py:377 | SQL table_info uses f-string (low risk — hardcoded table names) |
| 73 | database.py:1005 | SQL date arithmetic uses string interpolation |
| 74 | alert_manager.py:77 | All alert backends can fail silently |
| 75 | backtest_engine.py:9 | Misleading comment about run_backtest.py distinction |
| 76 | backfill_markets.py:214 | Random seed not set → non-deterministic backtests |

---

## Architecture Notes

### Strengths
- Multi-gate signal filtering (7 gates for AI probability)
- Half-Kelly with calibration-adaptive sizing
- Three-gate live trading safety system
- Circuit breaker with consecutive loss detection
- PM2 dedup prevents duplicate trades on restart
- Comprehensive test suite (801 tests)

### Design Risks to Monitor
- Bankroll is static (never re-synced to actual account balance)
- Position prices depend on scan cycle (no real-time WebSocket for positions)
- Exit orders bypass risk checks (by design, but no balance validation)
- Backtest system has multiple biases — do not use for live trading decisions
- Prompt injection possible via market descriptions from external APIs

---

## Fixes Applied in This Commit

1. **Order.price validator**: reject $0.00 (was `< 0`, now `<= 0`)
2. **Signal.edge validator**: reject NaN, infinity, values outside [-1, 1]
3. **Signal.market_price validator**: reject 0.0 and 1.0 (was inclusive, now exclusive)
4. **EnsembleForecast.edge validator**: reject NaN and infinity
5. **ForecastResult CI inversion**: now logs WARNING when auto-correcting
6. **Ensemble Brier weights**: fall back to equal weights when all scores = 1.0
7. **Whale basket**: guard against empty basket division by zero
8. **Obvious NO**: explicit guard for edge <= 0
9. **Test update**: NaN/inf edge tests now verify model-level rejection
