# VANGUARD V4B/V5 REWRITE — Apr 2, 2026

## What Changed

The original V4B was a classifier TRAINER (binary TBM labels, dual-track LGBM+TCN).
The original V5 was a 13-strategy router with SMC confluence, momentum, mean reversion, etc.

Both were replaced with simpler, production-ready alternatives:

### V4B: vanguard_scorer.py (INFERENCE ONLY)
- Does NOT train models. Models are pre-trained on Vast.ai and stored as .pkl files.
- Loads regressor ensembles per asset class
- Reads latest V3 features from `vanguard_factor_matrix`
- Ensemble predicts → `predicted_return` (positive = LONG, negative = SHORT)
- Writes to `vanguard_predictions` table

### V5: vanguard_selection_simple.py (SIMPLE RANKING)
- No strategy routers, no SMC, no consensus counting
- Ranks all predictions by |predicted_return|
- Stores top 30 LONG + 30 SHORT per asset class
- Displays top 5+5
- Writes to `vanguard_shortlist_v2` table

## Model Ensembles

### Equity (29 features from equity_regressor_meta.json)
```python
ensemble = 0.4 * lgbm_equity_regressor + 0.3 * ridge_equity_regressor + 0.3 * et_equity_regressor
```
- LGBM IC: +0.034 (1.94M training rows, 251 sessions)
- Ridge IC: +0.025
- ET IC: +0.025
- Features: session_vwap_distance, premium_discount_zone, gap_pct, session_opening_range_position,
  daily_drawdown_from_high, momentum_3bar, momentum_12bar, momentum_acceleration, atr_expansion,
  daily_adx, relative_volume, volume_burst_z, down_volume_ratio, effort_vs_result,
  rs_vs_benchmark_intraday, daily_rs_vs_benchmark, benchmark_momentum_12bar,
  cross_asset_correlation, session_phase, ob_proximity_5m, fvg_bullish_nearest,
  fvg_bearish_nearest, structure_break, liquidity_sweep, smc_premium_discount,
  htf_trend_direction, htf_structure_break, htf_fvg_nearest, htf_ob_proximity

### Forex (27 features from vanguard_forex_model_meta.json)
```python
ensemble = 0.5 * rf_forex_regressor + 0.5 * lgbm_forex_regressor
```
- RF IC: +0.049 (263K training rows, 176 sessions)
- LGBM IC: +0.037
- Features: same as equity minus relative_volume and effort_vs_result

## Key Finding: Regressor >> Classifier for Intraday
From Vast.ai A100 shootout (Session 8):
- Forex: Ridge IC +0.058, RF IC +0.049, LGBM IC +0.038 — all regressors PASS
- Forex TCN: IC -0.003 to +0.009 — FAIL on all feature subsets
- Equity LGBM Regressor: IC +0.031 — PASS (borderline)
- Equity TCN: IC -0.001 — FAIL
- Conclusion: Tree regressors work on 5m bars, TCN needs longer sequences (daily bars)

## Current Issue: Forex Predictions Near-Constant
After fixing NaN features and API key loading, forex predictions are still:
- All 35 SHORT, range -0.00005 to -0.000008
- No differentiation between pairs
- This is a MODEL QUALITY issue, not plumbing

Possible causes:
1. Feature contract mismatch: V3 produces features at different scale than training data
2. NaN→0 conversion creates uniform feature vectors
3. Models trained on historical data with different market regime

Investigation: compare `vanguard_factor_matrix` live feature distributions against
training data distributions from `vanguard_training_data`.

## Model File Locations
```
~/SS/Vanguard/models/
├── equity_regressors/
│   ├── lgbm_equity_regressor_v1.pkl
│   ├── ridge_equity_regressor_v1.pkl
│   ├── et_equity_regressor_v1.pkl
│   ├── lgbm_sw_equity_regressor_v1.pkl
│   └── meta.json
├── forex_regressors/
│   ├── rf_forex_regressor_v1.pkl
│   ├── lgbm_forex_regressor_v1.pkl
│   └── meta.json
├── equity_classifiers/  (available, not used in production)
│   ├── lgbm_equity_long_v1.pkl
│   ├── lgbm_equity_short_v1.pkl
│   ├── lgbm_long.pkl (universal)
│   └── lgbm_short.pkl (universal)
└── forex_classifiers/  (available, not used in production)
    ├── lgbm_forex_long_v1.pkl
    └── lgbm_forex_short_v1.pkl
```

## V7 Orchestrator Integration
`vanguard_orchestrator.py` now calls:
1. V1 → V2 → V3 (unchanged)
2. `vanguard_scorer.py` (was: vanguard_model_trainer.py)
3. `vanguard_selection_simple.py` (was: vanguard_selection.py)
4. V6 → V7 (unchanged)

Supports: `--single-cycle`, `--force-regime ACTIVE`, `--loop --interval 300`

## DB Tables

### vanguard_predictions
```sql
cycle_ts_utc TEXT, symbol TEXT, asset_class TEXT,
predicted_return REAL, direction TEXT, model_id TEXT, scored_at TEXT
PRIMARY KEY (cycle_ts_utc, symbol)
```

### vanguard_shortlist_v2
```sql
cycle_ts_utc TEXT, symbol TEXT, asset_class TEXT,
direction TEXT, predicted_return REAL, rank INTEGER,
model_id TEXT, created_at TEXT
PRIMARY KEY (cycle_ts_utc, symbol, direction)
```
