# PolyEdge Full Codebase Audit — 2026-03-28 (Revision 3)

Complete line-by-line audit of all 56 source files (~42K lines).
Three full audit passes completed. Issues marked **[FIXED]** were resolved.

---

## CRITICAL — Direct Money Loss or Crashes

### Models & Validation
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 1 | models.py:288 | Order.price validator allowed $0.00 → division by zero in Kelly | **[FIXED r1]** |
| 2 | models.py:228 | Signal.edge had no validator — NaN/infinity propagated to Kelly sizer | **[FIXED r1]** |
| 3 | models.py:423 | EnsembleForecast.edge had no validator — NaN/infinity possible | **[FIXED r1]** |
| 4 | models.py:246 | Signal.market_price allowed 0.0 and 1.0 → Kelly division by zero | **[FIXED r1]** |
| 5 | models.py:411 | CI inversion set both bounds equal instead of swapping → zero-width CI → false confidence → oversized positions | **[FIXED r2]** |
| 6 | models.py:434 | EnsembleForecast.final_probability had no validator — NaN/inf crashed Kelly sizer | **[FIXED r2]** |

### Ensemble & Forecasting
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 7 | ensemble.py:231 | Brier-weighted ensemble breaks when all scores=1.0 (weights don't normalize) | **[FIXED r1]** |
| 8 | ensemble.py:53 | CI width penalty uses abs() which masks inverted bounds → confidence inflated | **[FIXED r3]** |

### Data Layer
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 9 | whale_monitor.py:148 | Division by zero when whale basket is empty | **[FIXED r1]** |
| 10 | news_ingestion.py:111 | Division by zero when market question produces empty word set | Open |

### Strategies
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 11 | obvious_no.py:86 | Edge could be zero or negative without guard | **[FIXED r1]** |
| 12 | cross_arb.py:80 | Truthiness check `not market.yes_price` treated $0.00 as falsy → missed arb opportunities | **[FIXED r2]** |

### Execution
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 13 | position_manager.py:262 | Take-profit used `1.0 - avg_entry_price` — wrong for BUY_NO and SELL positions → stuck capital | **[FIXED r2]** |
| 14 | order_builder.py:110 | Market order with missing token price (0.0) silently clamped to $0.01 → wrong order price | **[FIXED r2]** |
| 79 | position_manager.py:330 | `_calculate_remaining_edge()` wrong for SELL and BUY_NO: used wrong price source, wrong underwater check (current < entry backwards for SELL), wrong upside formula | **[FIXED r3]** |

### Risk & Sizing
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 15 | kelly_sizer.py:84 | market_price >= 0.99 rejected valid high-probability trades (should be >= 1.0) | **[FIXED r2]** |
| 80 | kelly_sizer.py:168 | Calibration multiplier killed multi-contract positions: 5 contracts × 0.10 = 0, treated same as single-contract zero-conviction | **[FIXED r3]** |

---

## HIGH — Logic Errors, Security, Silent Failures

### API Clients
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 16 | kalshi_client.py:138 | Rate limit (429) returns None silently instead of raising | Open |
| 17 | websocket_client.py:356 | WebSocket auth failure returns empty dict silently — fills never received | Open |
| 18 | polymarket_client.py:102 | Balance conversion heuristic (`/1e6 if >1000`) is fragile | Open |
| 19 | polymarket_client.py:113 | Price parsing can crash on malformed API response (no try/except) | Open |
| 20 | kalshi_client.py:286 | get_balance() doesn't validate non-negative balance | Open |

### Security
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 21 | prompt_templates.py:230 | Prompt injection risk: market question/description injected unsanitized | Open |
| 22 | claude_forecaster.py:102 | Prompt injection risk: news context from untrusted sources injected raw | Open |
| 23 | kalshi_client.py:52 | Private key file opened without checking file permissions (should be 600) | Open |

### Execution
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 24 | main.py:283 | Exit orders bypass risk engine entirely (no balance/circuit breaker check) | Open |
| 25 | order_router.py:206 | Three-gate live safety: Gate 3 bypassed permanently after first confirmation | Open |
| 26 | fill_tracker.py:254 | Order.size mutation during clamp breaks partial fill tracking | Open |
| 27 | position_manager.py:156 | Position price update accepted zero for the active direction → false stop-loss triggers | **[FIXED r2]** |

### Database
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 28 | database.py:245 | Foreign keys disabled globally, no app-level enforcement | Open |

### Backtest
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 29 | backtest_engine.py:80-120 | Lookahead bias: synthetic forecasts use known outcomes | Open |
| 30 | backtest_engine.py:257 | Survivorship bias: only settled markets included | Open |
| 31 | backtest_engine.py:372 | Fees not simulated in backtest | Open |

### Configuration
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 32 | config.py:176 | Missing ANTHROPIC_API_KEY not caught until first Claude call | Open |
| 33 | config.py:181 | Missing Polymarket private key not caught until first PM order | Open |
| 34 | config.py:154 | Logging level not validated against valid Python levels | Open |
| 35 | config.py:147 | Database path not validated for empty string | Open |
| 36 | settings.yaml:69 | cross_check_disagreement_threshold (0.12) differs from config.py default (0.15) | Open |

### Async / Concurrency
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 37 | polymarket_client.py:75 | Uses deprecated asyncio.get_event_loop() | Open |
| 38 | websocket_client.py:243 | Price callbacks awaited sequentially — slow callback blocks feed | Open |
| 39 | websocket_client.py:159 | Reconnect loop spins forever on permanent auth failure | Open |

---

## MEDIUM — Edge Cases, Inefficiencies, Data Quality

### Risk & Sizing
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 40 | risk_engine.py:140 | Edge == probability_estimate allowed (implies market_price=0) | Open |
| 41 | risk_engine.py:41 | Cooldown persistence doesn't validate datetime format from DB | Open |
| 42 | kelly_sizer.py:34 | fee_rate hardcoded to 0.0 — correct for event markets but fragile | Open |

### Data Enrichment
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 43 | fedwatch.py:69 | FedWatch regex fragile — CME page redesign silently disables it | Open |
| 44 | cleveland_fed.py:63 | Cleveland Fed parsing fragile — site redesign silently disables it | Open |
| 45 | metaculus_client.py:107 | Metaculus disabled for entire session if first probe fails | Open |
| 46 | news_ingestion.py:60 | _seen_urls set grows unbounded → memory leak on long runs | Open |
| 47 | polymarket_cross_ref.py:67 | Price parsing falls back to stale bestBid when outcomePrices malformed | Open |

### Market Scanning
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 48 | market_discovery.py:191 | Unknown market status (e.g., "resolving") silently dropped | Open |
| 49 | polymarket_discovery.py:43 | Outcome prices not bounds-checked after parsing | Open |
| 50 | market_scanner.py:130 | log10(volume) crashes on volume=0 | Open |
| 51 | polymarket_scanner.py:89 | Price sum tolerance 0.90-1.10 too loose (10% deviation allowed) | Open |
| 52 | market_discovery.py:186 | Negative spread (bid > ask) not validated | Open |

### Calibration
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 53 | calibration.py:115 | Records with bad actual_outcome silently skipped (no logging) | Open |
| 54 | claude_forecaster.py:449 | Default CI (±0.15) creates false precision when Claude omits bounds — widened to ±0.25 | **[FIXED r3]** |
| 55 | calibration.py:224 | Win rate uses flat 0.5 threshold regardless of edge size | Open |

### Execution Details
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 56 | main.py:212 | Zero-price fallback creates invalid Market with price=0 | Open |
| 57 | main.py:109 | Bankroll read once at startup, never re-synced to actual balance | Open |
| 58 | main.py:646 | Timeout error message hardcoded "(>5 minutes)" vs config value | Open |

### Backtest Details
| # | File:Line | Issue | Status |
|---|-----------|-------|--------|
| 59 | backtest_engine.py:314+387 | Positions can be resolved twice (mid-loop + final loop) | Open |
| 60 | backtest_engine.py:322 | Division by zero: pnl / (size * price) when size=0 | **[FIXED r3]** |
| 61 | backfill_markets.py:198 | volume_1h is trade count, not dollar volume | Open |
| 62 | backfill_markets.py:262 | Synthetic snapshots use linear drift (unrealistic) | Open |

---

## LOW — Code Quality, Minor Edge Cases

| # | File:Line | Issue |
|---|-----------|-------|
| 63 | models.py:132 | MarketToken.price defaults to 0.0 (should require explicit value) |
| 64 | market_graph.py:45 | ChromaDB failure falls back to weak keyword matching silently |
| 65 | market_graph.py:193 | Keyword tokenization misses hyphenated terms |
| 66 | cache.py:27 | TTL uses time.monotonic() — expired entries only cleaned on access |
| 67 | news_ingestion.py:34 | Timezone-naive published dates treated as UTC (may be EST) |
| 68 | news_researcher.py:337 | Context truncated by chars not tokens (~800 tokens approximation) |
| 69 | resolution_tracker.py:133 | Resolution inferred from yes_price>0.5 — ambiguous at exactly 0.5 |
| 70 | calibration_analyzer.py:150 | Base rate requires ≥8 samples (hard cutoff, no confidence interval) |
| 71 | order_builder.py:161 | Price clamping is silent (no warning logged) |
| 72 | dashboard.py:194 | Daily loss calculation uses confusing min(0, pnl) pattern |
| 73 | daily_report.py:76 | Win rate calculation fragile (works but unclear) |
| 74 | database.py:377 | SQL table_info uses f-string (low risk — hardcoded table names) |
| 75 | database.py:1005 | SQL date arithmetic uses string interpolation |
| 76 | alert_manager.py:77 | All alert backends can fail silently |
| 77 | backtest_engine.py:9 | Misleading comment about run_backtest.py distinction |
| 78 | backfill_markets.py:214 | Random seed not set → non-deterministic backtests |

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
- Gate 3 live trading confirmation is per-session not per-trade

---

## Fixes Applied — Revision 1 (2026-03-28)

1. **Order.price validator**: reject $0.00 (was `< 0`, now `<= 0`)
2. **Signal.edge validator**: reject NaN, infinity, values outside [-1, 1]
3. **Signal.market_price validator**: reject 0.0 and 1.0 (was inclusive, now exclusive)
4. **EnsembleForecast.edge validator**: reject NaN and infinity
5. **ForecastResult CI inversion**: now logs WARNING when auto-correcting
6. **Ensemble Brier weights**: fall back to equal weights when all scores = 1.0
7. **Whale basket**: guard against empty basket division by zero
8. **Obvious NO**: explicit guard for edge <= 0
9. **Test update**: NaN/inf edge tests now verify model-level rejection

## Fixes Applied — Revision 2 (2026-03-28)

1. **CI inversion swap**: was setting both bounds equal (zero-width CI); now properly swaps low/high via `info.data["confidence_low"]`
2. **EnsembleForecast.final_probability validator**: reject NaN, infinity, values outside [0, 1]
3. **Cross-arb truthiness**: `not market.yes_price` → `market.yes_price <= 0` (no longer treats $0.00 as falsy)
4. **Take-profit direction-aware**: formula now uses `entry * size` for BUY_NO/SELL, `(1-entry) * size` for BUY_YES
5. **Position price validation**: skip update when the active direction's price is zero (prevents false stop-loss)
6. **Kelly market_price bound**: changed `>= 0.99` to `>= 1.0` — no longer rejects valid high-probability trades
7. **Order builder zero-price**: market orders now raise ValueError when token price is 0.0 instead of silently clamping to $0.01

## Fixes Applied — Revision 3 (2026-03-28)

1. **Remaining edge direction fix**: `_calculate_remaining_edge()` now handles all four directions correctly — SELL positions check `current > entry` (underwater when price rises), BUY positions check `current < entry`. Each direction uses the correct token price (yes_price or no_price) and correct upside formula.
2. **Kelly calibration multiplier**: Only kills trades when calibration ≤25% AND original sizing was 1 contract (minimal conviction). Multi-contract positions (e.g., 5 × 0.10 = 0) now floor to 1 contract instead of being silently dropped.
3. **Ensemble CI masking**: Removed `abs()` from CI width calculation — inverted bounds now produce conservative penalty (width ≤ 0 → penalty 0) instead of being masked as a wide confident interval.
4. **Default CI widened**: When Claude omits confidence bounds, fallback changed from ±0.15 to ±0.25 — produces appropriately humble ensemble weighting instead of false precision.
5. **Backtest div-by-zero**: Combined `t.price > 0` and `t.size > 0` into a single guard before computing realized edge ratio.
