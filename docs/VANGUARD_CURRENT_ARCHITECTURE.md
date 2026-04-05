# Vanguard Intraday System — Current Architecture

## Overview

Vanguard is a multi-asset intraday trading system that runs on 5-minute bars.
It produces trade picks every 5 minutes during market hours. Currently handles
US equities (via Alpaca) and forex/crypto/metals (via Twelve Data).

## Pipeline Flow

```
Every 5 minutes during market hours:

V1: Data Collection (66.6s — bottleneck is Twelve Data 62s)
│   ├── Alpaca REST: poll 1m bars for ~215 active equities (3.4s)
│   ├── Alpaca WebSocket: background stream → vanguard_bars_1m
│   ├── Twelve Data: poll 1m bars for ~90 non-equity symbols (62s)
│   ├── Aggregate: 1m → 5m (0.15s)
│   └── Aggregate: 5m → 1h (1.0s)
│
V2: Prefilter / Health Monitor (0.66s)
│   ├── Read latest bars for all instruments
│   ├── Check: session active? data fresh? volume OK? spread OK? warmed up?
│   ├── Classify: ACTIVE, STALE, CLOSED, LOW_VOLUME, HALTED, WARM_UP
│   └── Output: ~48 ACTIVE survivors (varies by time of day)
│
V3: Factor Engine (2.39s)
│   ├── Compute 35 features for each ACTIVE survivor
│   ├── Features grouped into 10 modules:
│   │   ├── Price Location (4): vwap_distance, premium_discount, gap_pct, opening_range
│   │   ├── Momentum (4): momentum_3bar, momentum_12bar, acceleration, atr_expansion
│   │   ├── Volume (4): relative_volume, volume_burst_z, down_volume_ratio, effort_vs_result
│   │   ├── Relative Strength (4): rs_intraday, rs_daily, benchmark_momentum, cross_asset_corr
│   │   ├── Session (3): session_phase, time_in_session_pct, bars_since_open
│   │   ├── Daily Bridge (3): daily_adx, daily_drawdown, daily_conviction
│   │   ├── SMC 5m (6): ob_proximity, fvg_bull, fvg_bear, structure_break, liquidity_sweep, premium_discount
│   │   └── HTF 1h (4): htf_trend, htf_structure_break, htf_fvg, htf_ob_proximity
│   └── Write to: vanguard_factor_matrix table
│
V4B: Scorer (1.60s)
│   ├── Load pre-trained regressor .pkl files per asset class
│   ├── Equity ensemble: 0.4×LGBM + 0.3×Ridge + 0.3×ExtraTrees
│   ├── Forex ensemble: 0.5×RF + 0.5×LGBM
│   ├── Extract model's expected features from V3 output
│   ├── Fill missing features with 0.0
│   ├── Coerce all features to float64
│   ├── ensemble.predict(features) → predicted_return (float)
│   ├── Direction: positive = LONG, negative = SHORT
│   └── Write to: vanguard_predictions table
│
V5: Selection (0.02s)
│   ├── Read predictions for latest cycle
│   ├── Per asset class:
│   │   ├── LONG: predicted_return > 0, sorted DESC, top 30
│   │   └── SHORT: predicted_return < 0, sorted ASC, top 30
│   └── Write to: vanguard_shortlist_v2 table
│
V6: Risk Filters (0.49s)
│   ├── Load all account profiles (TTP, FTMO, TopStep)
│   ├── Check instrument scope (TTP = US equity only, FTMO = forex/CFDs)
│   ├── Check position limits, daily loss, max drawdown
│   └── Write to: vanguard_tradeable_portfolio table
│
V7: Executor (mode=off currently)
    ├── Forward-track approved trades (log without executing)
    └── When mode=paper or mode=live: execute via webhook/API
```

## Database Schema (key tables)

### vanguard_bars_1m
```sql
symbol TEXT, bar_ts_utc TEXT, open REAL, high REAL, low REAL, close REAL,
volume INTEGER, tick_volume INTEGER, asset_class TEXT, data_source TEXT
PK: (symbol, bar_ts_utc)
```

### vanguard_factor_matrix (V3 output)
```sql
-- 35 feature columns + metadata
cycle_ts_utc TEXT, symbol TEXT, asset_class TEXT,
session_vwap_distance REAL, premium_discount_zone REAL, gap_pct REAL,
... (35 features total) ...
nan_ratio REAL
PK: (cycle_ts_utc, symbol)
```

### vanguard_predictions (V4B output)
```sql
cycle_ts_utc TEXT, symbol TEXT, asset_class TEXT,
predicted_return REAL, direction TEXT, model_id TEXT, scored_at TEXT
PK: (cycle_ts_utc, symbol)
```

### vanguard_shortlist_v2 (V5 output)
```sql
cycle_ts_utc TEXT, symbol TEXT, asset_class TEXT,
direction TEXT, predicted_return REAL, rank INTEGER,
model_id TEXT, created_at TEXT
PK: (cycle_ts_utc, symbol, direction)
```

## Model Loading Code (V4B scorer — simplified)

```python
# vanguard_scorer.py (key logic)

ASSET_CONFIGS = {
    "equity": {
        "model_dir": "models/equity_regressors",
        "models": {
            "lgbm": {"file": "lgbm_equity_regressor_v1.pkl", "weight": 0.4},
            "ridge": {"file": "ridge_equity_regressor_v1.pkl", "weight": 0.3},
            "et":    {"file": "et_equity_regressor_v1.pkl",    "weight": 0.3},
        },
        "ensemble": "weighted",
    },
    "forex": {
        "model_dir": "models/forex_regressors",
        "models": {
            "rf":   {"file": "rf_forex_regressor_v1.pkl",   "weight": 0.5},
            "lgbm": {"file": "lgbm_forex_regressor_v1.pkl", "weight": 0.5},
        },
        "ensemble": "simple_avg",
    },
}

def score_survivors():
    # Load latest V3 features from vanguard_factor_matrix
    features_df = pd.read_sql("SELECT ... FROM vanguard_factor_matrix WHERE cycle = latest")
    
    for asset_class in features_df['asset_class'].unique():
        models, feature_cols = load_models(asset_class)  # from meta.json
        subset = features_df[features_df['asset_class'] == asset_class]
        
        # Build feature matrix — V3 has 35 cols, model expects 27-29
        X = np.zeros((len(subset), len(feature_cols)))
        for i, col in enumerate(feature_cols):
            if col in subset.columns:
                vals = pd.to_numeric(subset[col], errors='coerce').fillna(0.0).values
                X[:, i] = vals
        
        # Ensemble predict
        predictions = []
        for name, m in models.items():
            pred = m["model"].predict(X)
            predictions.append(pred * m["weight"])
        predicted_return = sum(predictions)  # weighted sum
        
        # Direction from sign
        direction = np.where(predicted_return > 0, "LONG", "SHORT")
```

## Training Pipeline (V4A — how models were trained)

```python
# Training was done on Vast.ai A100, NOT on this machine
# Training data came from vanguard_training_backfill.py replay:

# 1. Load historical 5m bars from vanguard_bars_5m
# 2. Replay V3 factor computation on each bar (historical features)
# 3. Compute forward_return label: (close[t+6] - close[t]) / close[t]
#    where t+6 = 6 bars ahead = 30 minutes
# 4. Write to vanguard_training_data table
# 5. Upload to Vast.ai as CSV
# 6. Train with walk-forward validation (expanding window, session splits)
# 7. Save .pkl files, download to Mac

# KEY CONCERN: The V4A replay and V3 live compute are DIFFERENT code paths
# V4A: stages/vanguard_training_backfill.py (or _fast.py)
# V3:  stages/vanguard_factor_engine.py
# These SHOULD produce identical features but may not
```

## What Works vs What Doesn't

| Component | Status | Evidence |
|---|---|---|
| V1 Data Collection | ✅ Works | Fresh bars in DB, equity + forex |
| V2 Prefilter | ✅ Works | 48 ACTIVE survivors, session-aware |
| V3 Factor Engine | ⚠️ Partial | 0% NaN after fix, but feature values may differ from training |
| V4B Scorer | ⚠️ Loads & runs | Models load, predict() returns values, but values are near-constant |
| V5 Selection | ✅ Works | Ranks correctly based on predictions |
| V6 Risk Filters | ✅ Works | Approves TTP equity, rejects forex for TTP |
| **Model predictions** | ❌ Problem | Near-constant for forex, narrow range for equity |

## The Core Question

The models have validated IC (+0.034 to +0.049) in walk-forward on training data.
But in production they produce near-constant outputs. Why?

Three hypotheses:
1. **Feature distribution mismatch** between training (V4A historical replay) and production (V3 live)
2. **Cross-sectional ranking needed** — raw predicted_return is too small, needs percentile normalization
3. **Ensemble averaging cancels signal** — individual models disagree, average collapses to zero

The fix could be one or more of:
- Verify feature alignment (compare training vs production distributions)
- Apply cross-sectional percentile ranking in V5 instead of raw predicted_return
- Use individual model predictions instead of ensemble
- Retrain models on production V3 features (not V4A replay features)
- Add feature standardization (z-score) before inference
