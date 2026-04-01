# PolyEdge Audit & Improvement Log

## Section 1: Baseline — 2026-04-01 ~09:00 PT

### Test Suite
- **Result**: 1469 passed, 1 failed, 1 skipped, 102 warnings
- **Duration**: 108.42s
- **Failing test**: `test_confidence_gate_boundary` — mock not applied, real Claude API call leaks through (401 auth error)

### Backtest
- **Result**: 0 trades executed — circuit breaker triggers immediately
- **Issue**: Backtest loads circuit breaker state from live DB (`halted=True, consecutive_losing_days=3`)
- **CB trigger**: "Max drawdown hit: 90.0% from peak $5000.00 (limit 20%)" — but no trades occurred, so this is stale state
- **CB skipped**: 43,400 potential trades

### Backtest with --validate
- Same 0-trade result
- All Phase 3 exit criteria FAIL except max_drawdown (trivially passes with 0 trades)

### Database Stats
| Table | Count |
|-------|-------|
| Markets | 1,985 |
| Resolved | 647 |
| Calibration records | 13 |
| Trades | 22 |
| Snapshots | 43,400 |

### Current Settings (key params)
- `kelly_fraction`: 0.5 (DANGEROUS — should be 0.25)
- `daily_loss_limit_pct`: 0.15 (too generous — should be 0.08)
- `bankroll`: $5,000
- `max_position_pct`: 0.05
- `min_edge_ai`: 0.05
- `mode`: paper

### Log Analysis
- Bot is actively running (logs from today)
- Claude API daily token usage: 1,757,195 — exceeds soft limit of 1,000,000
- News researcher has date parsing issues (ISO 8601 dates failing)
- No ERROR-level issues besides token budget warnings

---
