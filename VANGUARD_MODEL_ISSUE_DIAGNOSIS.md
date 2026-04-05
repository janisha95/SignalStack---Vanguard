# Vanguard Intraday Model Prediction Issue — Diagnosis Document

## The Problem

We have trained regressor models (Random Forest, LGBM, Ridge, ExtraTrees) on 5-minute intraday bar data that show good IC (Information Coefficient) in walk-forward validation. But when deployed in the live production pipeline, the models produce near-constant predictions with almost no differentiation between instruments. All forex predictions are SHORT, all in a narrow band of -0.00005 to -0.000008. Equity predictions show some differentiation but are still very small (0.00001 to 0.00012 range).

## The Models (all properly trained, verified 300-500 trees)

### Equity Regressors (29 features, target = forward_return)
| Model | File Size | Trees/Estimators | Walk-Forward IC |
|---|---|---|---|
| LGBM | 485KB | 300 trees | +0.034 |
| LGBM shallow wide | 285KB | 300 trees | +0.032 |
| Ridge | 1.6KB | N/A (linear) | +0.025 |
| ExtraTrees | 560KB | 100 estimators | +0.025 |
| **Ensemble** (0.4×LGBM + 0.3×Ridge + 0.3×ET) | | | **+0.034** |

### Forex Regressors (27 features, target = forward_return)
| Model | File Size | Trees/Estimators | Walk-Forward IC |
|---|---|---|---|
| Random Forest | 948KB | 200 estimators | +0.049 |
| LGBM | 251KB | 300 trees | +0.037 |
| **Ensemble** (avg RF + LGBM) | | | **~0.043** |

### Training Data
- Equity: 1,943,309 rows, 251 sessions, 29 features
- Forex: 263,107 rows, 176 sessions, 27 features
- Target: `forward_return` = (close[t+6] - close[t]) / close[t] where t+6 = 30 minutes ahead on 5m bars
- Walk-forward: expanding window, session-based splits, purged (no leakage)

## Production Pipeline (where it breaks)

```
V1: Data Collection
  - Alpaca WebSocket + REST → equity 1m/5m bars (~215 symbols)
  - Twelve Data REST → forex/crypto/metal 1m/5m bars (~90 symbols)
  
V2: Prefilter/Health Check
  - Classifies instruments: ACTIVE, STALE, LOW_VOLUME, CLOSED, etc.
  - Only ACTIVE instruments proceed (~48 survivors during forex hours)

V3: Factor Engine (vanguard_factor_engine.py)
  - Computes 35 features for each survivor from latest 5m bars
  - Features stored in vanguard_factor_matrix table
  - NaN rate was 19.7% (fixed to 0% after NaN→0 fallback patch)
  - Time: ~2.4 seconds

V4B: Scorer (vanguard_scorer.py — NEW, replaced old classifier)
  - Loads pre-trained .pkl regressor models
  - Reads features from vanguard_factor_matrix
  - Maps V3's 35 features to the model's expected 27-29 features
  - Missing features filled with 0.0
  - Runs ensemble predict → predicted_return (float)
  - Direction: positive = LONG, negative = SHORT
  - Writes to vanguard_predictions table

V5: Selection (vanguard_selection_simple.py — NEW)
  - Reads vanguard_predictions
  - Ranks by |predicted_return| descending
  - Stores top 30 LONG + 30 SHORT per asset class
  - No minimum threshold (removed after it blocked everything)
```

## Symptoms

### Forex Predictions (35 scored, latest cycle)
```
ALL 35 predictions are SHORT
Range: -0.00005099 to -0.00000798
Mean: -0.00002858
Std: ~0.00001500
No LONG predictions at all
```

### Equity Predictions (183 scored, latest cycle)
```
18 LONG, 165 SHORT (heavily short-biased)
LONG range: +0.00000009 (AMD) to +0.00007382 (RKLZ)
SHORT range: -0.00011799 (GIS) to -0.00000001
The equity predictions at least differentiate direction
```

## What We've Investigated

### 1. NaN Features (FIXED)
- 7 of 35 V3 features were NULL for all forex symbols
- Root cause: SMC features and session features returning None
- Fix: NaN→0 fallback in V3 factor engine
- Result: NaN rate dropped from 19.7% to 0.0%
- **BUT predictions remained near-constant after fix**

### 2. Model File Integrity (VERIFIED OK)
- All models are properly trained: 200-500 trees, file sizes 250KB-950KB
- `model.booster_.num_trees()` confirms 300-500 trees for each LGBM
- Not an undertrained model issue

### 3. Feature Alignment
This is the SUSPECTED root cause but NOT yet confirmed.

**Training features** were computed by V4A training backfill on historical 5m bars.
**Production features** are computed by V3 factor engine on live 5m bars.

These are **different code paths**. V4A and V3 are separate files that may compute
features differently:
- V4A: `stages/vanguard_training_backfill.py` (or `_fast.py`)
- V3: `stages/vanguard_factor_engine.py`

Potential mismatches:
- Different normalization (z-score vs raw vs rank)
- Different lookback windows
- Different NaN handling (V4A might have had real values where V3 produces 0.0)
- Different session boundaries
- Features computed at different times of day (training = historical replay, production = live)

### 4. Feature Distribution Comparison (NOT YET DONE)
We have NOT compared:
- Training feature distributions (from vanguard_training_data table)
- Live feature distributions (from vanguard_factor_matrix table)

This comparison would definitively prove or disprove the feature alignment theory.

### 5. Regressor Output Scale
Regressors predict `forward_return` which is a very small number for 5-minute bars:
- Typical 5m return: ±0.001 to ±0.005 (0.1% to 0.5%)
- Forex 5m return: ±0.0001 to ±0.001 (0.01% to 0.1%)

The model predictions (-0.00005 to +0.00007) are WITHIN the expected range for
5-minute returns. The issue isn't that predictions are "too small" — it's that
they don't differentiate between instruments. A good model should predict EURUSD
differently from GBPJPY.

## Key Questions for Investigation

1. **Are the features the same between training and production?**
   Compare feature distributions from `vanguard_training_data` vs live `vanguard_factor_matrix`.
   If distributions are wildly different, that's the root cause.

2. **Should we use cross-sectional ranking instead of raw predictions?**
   Convert raw predicted_return to percentile rank within each cycle timestamp.
   This normalizes the tiny values into 0.0-1.0 range with guaranteed spread.

3. **Should we use classification (binary: up > X% or not) instead of regression?**
   The training IC was measured on regression. But a classifier might produce
   more actionable probabilities (0.0 to 1.0) with natural spread.

4. **Is the ensemble averaging collapsing the signal?**
   If RF says +0.0001 and LGBM says -0.0001, the average is 0.0.
   Try using individual models instead of the ensemble.

5. **Should features be standardized before inference?**
   If training data had features normalized (zero mean, unit variance) but
   production does not, the model sees different input distributions.

## Files and Paths

| Component | Path |
|---|---|
| V3 Factor Engine | `~/SS/Vanguard/stages/vanguard_factor_engine.py` |
| V4A Training Backfill | `~/SS/Vanguard/stages/vanguard_training_backfill.py` |
| V4B Scorer | `~/SS/Vanguard/stages/vanguard_scorer.py` |
| V5 Selection | `~/SS/Vanguard/stages/vanguard_selection_simple.py` |
| Equity regressor models | `~/SS/Vanguard/models/equity_regressors/*.pkl` |
| Forex regressor models | `~/SS/Vanguard/models/forex_regressors/*.pkl` |
| Equity meta | `~/SS/Vanguard/models/equity_regressors/meta.json` |
| Forex meta | `~/SS/Vanguard/models/forex_regressors/meta.json` |
| Vanguard DB | `~/SS/Vanguard/data/vanguard_universe.db` |
| Training data table | `vanguard_training_data` in DB |
| Live features table | `vanguard_factor_matrix` in DB |
| Predictions table | `vanguard_predictions` in DB |

## Feature Lists

### Equity Model Expects (29 features):
session_vwap_distance, premium_discount_zone, gap_pct, session_opening_range_position,
daily_drawdown_from_high, momentum_3bar, momentum_12bar, momentum_acceleration,
atr_expansion, daily_adx, relative_volume, volume_burst_z, down_volume_ratio,
effort_vs_result, rs_vs_benchmark_intraday, daily_rs_vs_benchmark,
benchmark_momentum_12bar, cross_asset_correlation, session_phase, ob_proximity_5m,
fvg_bullish_nearest, fvg_bearish_nearest, structure_break, liquidity_sweep,
smc_premium_discount, htf_trend_direction, htf_structure_break, htf_fvg_nearest,
htf_ob_proximity

### Forex Model Expects (27 features):
Same as equity minus: relative_volume, effort_vs_result

### V3 Factor Engine Produces (35 features):
The model's expected features are a SUBSET of V3's 35. The scorer extracts
only the columns the model expects and fills missing ones with 0.0.

## What Grok Suggested

Grok's initial analysis suggests the core issue is that raw regressor outputs
(tiny floats like ±0.00005) should be converted to cross-sectional percentile
ranks within each cycle timestamp before being used for selection. This would:
- Normalize the scale (0.0 to 1.0 instead of ±0.00005)
- Guarantee spread across instruments
- Make direction assignment meaningful (top quintile = LONG, bottom = SHORT)

The question is whether this masks a deeper feature alignment issue or genuinely
solves the problem. If the model's cross-sectional ordering is correct (highest
predicted return = best candidate) but the magnitude is wrong, ranking fixes it.
If the ordering itself is wrong (all predictions are near-identical noise), ranking
just picks random instruments.
