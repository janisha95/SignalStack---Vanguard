# Vanguard Model Status — Updated Apr 2, 2026

## RESOLVED: Prediction Collapse Issue

The original issue (near-constant predictions, all-SHORT forex) is FIXED:
1. **Cross-sectional standardization** in V4B before predict() — aligns live features with training distribution
2. **Cross-sectional percentile ranking** in V5 — converts tiny predicted_returns to 0.01-0.99 edge_scores
3. **V6 readiness gate** updated to use edge_score instead of old classifier ml_prob

**Current results:** edge_score 0.02-0.99, LONG% = 44%, equity 30L+28S, forex 7L+7S

## REMAINING: Train-Serving Skew (Root Cause Still Exists)

The patches above MASK the skew — they don't eliminate it. The root cause:
- **Training features** computed by V4A (`vanguard_training_backfill.py`)
- **Production features** computed by V3 (`vanguard_factor_engine.py`)
- These are DIFFERENT CODE PATHS that should produce identical features but may not

### Permanent Fix: Shared Feature Module
Create `vanguard_feature_computer.py` imported by BOTH V3 and V4A.
One function, two callers. Skew impossible.
Status: CC prompt NOT yet written. Weekend task for Opus.

### Diagnostic: Feature Distribution Comparison
Still NOT done. Would definitively confirm/deny skew.
```sql
-- Compare training vs production feature distributions
-- Training (from V4A replay):
SELECT AVG(momentum_3bar), STDEV(momentum_3bar),
       AVG(atr_expansion), STDEV(atr_expansion)
FROM vanguard_training_data WHERE asset_class = 'equity';

-- Production (from V3 live):
SELECT AVG(momentum_3bar), STDEV(momentum_3bar),
       AVG(atr_expansion), STDEV(atr_expansion)
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
AND asset_class = 'equity';
```
If distributions are wildly different, skew is confirmed.

## Missing Models

| Asset Class | Training Data | Model | Status |
|---|---|---|---|
| metal | Yes (Twelve Data bars) | None | CC prompt written, not run |
| energy | Yes (Twelve Data bars) | None | CC prompt written, not run |
| index | Partial | None | CC prompt written, not run |
| crypto | Yes (14 symbols) | None | V3 features computed but no scorer |

## Work Split: Opus vs GPT/Grok

### Opus (remaining 13% usage — architecture only)
- Shared feature module spec (permanent skew fix)
- Model training prompts (metal/energy/index)
- MT5 executor architecture
- Session handoff documentation

### GPT/Grok (unlimited — implementation)
- V6 risk rules per account (copy from prop firm websites)
- UI/frontend for Vanguard picks
- MT5 executor implementation
- IBKR equity streaming enablement
- ETF exclusion for Vanguard (copy from Meridian)
- Forward tracking analysis
- Bug fixes, SQL queries, debugging

### Codex (implementation tasks)
- Index/metals model training (prompt ready)
- IBKR equity streaming (prompt ready)
- Shared feature module (needs Opus spec)
- MT5 executor (needs Opus spec)
