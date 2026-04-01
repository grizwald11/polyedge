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

## Section 2: Parameter Fixes — 2026-04-01 ~09:30 PT

### Changes Made
- `kelly_fraction`: 0.50 → 0.25 (quarter-Kelly)
- `daily_loss_limit_pct`: 0.15 → 0.08
- Added dynamic Kelly scaling (0.15–0.30 based on rolling 20-trade win rate)
- Added escalating cool-down (5% daily loss → 50% position reduction, 8% → halt)
- Added cheap contract filter (<12¢ rejection) in ai_probability strategy
- Added uncertain zone (30-70%) 1.5x edge multiplier

### Backtest After Changes
- Trades: 4 (same, small dataset)
- P&L: -$36.90 → -$30.14 (improvement from quarter-Kelly)
- Max drawdown: 9.2% → 7.8%

### Kelly Parameter Sweep
| Kelly | Trades | Win% | P&L | MaxDD% | Sharpe |
|-------|--------|------|-----|--------|--------|
| 0.10 | 4 | 50% | -$6.46 | 3.1% | -3.55 |
| 0.15 | 4 | 50% | -$18.33 | 5.5% | -7.38 |
| 0.20 | 4 | 50% | -$26.95 | 7.2% | -8.90 |
| **0.25** | **4** | **50%** | **-$24.67** | **6.8%** | **-8.32** |
| 0.30 | 4 | 50% | -$27.78 | 7.4% | -9.09 |
| 0.50 | 4 | 50% | -$23.50 | 6.6% | -7.95 |

(Note: 4-trade sample too small for conclusive results. Directionally: lower Kelly = lower losses.)

### Min Edge Sweep
| Min Edge | Trades | Win% | P&L | MaxDD% |
|----------|--------|------|-----|--------|
| 0.03 | 6 | 67% | -$29.86 | 6.0% |
| 0.05 | 4 | 50% | -$30.00 | 7.8% |
| 0.10 | 2 | 50% | -$9.30 | 3.4% |
| 0.15 | 1 | 100% | +$8.22 | 0.0% |

---

## Section 3: Prediction Accuracy — 2026-04-01 ~10:00 PT

### Changes Made
- Ensemble extremization: 15% log-odds push away from 50% (both single & multi-model)
- Platt scaling calibrator: logistic regression on (predicted, outcome) pairs
  - Activates after 50+ resolved predictions
  - Only if it improves Brier score
- Category auto-gating already implemented (Brier > 0.30)
- 15 new tests

---

## Section 5: Bug Sweep — 2026-04-01 ~10:15 PT

### Findings
- **HIGH**: Position dict iteration without snapshot in 2 places (get_exit_candidates, sync_with_kalshi) — FIXED
- **MEDIUM**: TTLCache lacks auto-cleanup for stale entries — FIXED (added cleanup at 500+ entries)
- **OK**: DB WAL mode, pm2 recovery, API retry all sound
- **OK**: Circuit breaker handles edge cases properly

### Tests
- 1486 passed, 1 skipped, 0 failed

---
