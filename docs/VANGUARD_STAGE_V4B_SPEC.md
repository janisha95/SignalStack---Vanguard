# STAGE V4B: Vanguard Model Trainer — vanguard_model_trainer.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 28, 2026
**Depends on:** V4A (vanguard_training_data)

---

## What Stage V4B Does

Trains intraday prediction models using a unified framework that supports
TWO model tracks trained simultaneously:

- **Track A: LightGBM** — 2 classifiers (long + short), trains on Mac in minutes
- **Track B: TCN dual-head** — 1 neural net (long prob + short prob), trains on GPU

Both tracks use the SAME training data, SAME walk-forward splits, SAME
evaluation metrics. Results are directly comparable. Config decides which
model(s) go live.

---

## Two Tracks, One Framework

### Track A: LightGBM (fast iteration, no GPU)

| Model | Label | Features | Training Time |
|---|---|---|---|
| lgbm_long | label_long | All 35 | ~30-60 sec per window |
| lgbm_short | label_short | All 35 | ~30-60 sec per window |

Both models get ALL 35 features. LightGBM automatically learns which
features matter for each side via gradient boosting. No manual feature
subsetting — let the algorithm decide.

Feature importance is free with LightGBM — after training, you'll see
which features the short model actually uses vs the long model. Data-driven,
not guessing.

### Track B: TCN Dual-Head (sequence model, GPU required)

| Model | Output | Features | Training Time |
|---|---|---|---|
| tcn_dual | long_prob + short_prob | All 35 × 64 bars | ~1-4 hours on GPU |

Single neural network with:
- Shared TCN trunk: 64-bar lookback × 35 features
- Long head: sigmoid → P(long win)
- Short head: sigmoid → P(short win)
- Trained with combined loss: BCE_long + BCE_short

The dual-head means one training run produces both directional predictions.
The shared trunk learns common market structure, heads specialize on direction.

Architecture:
```
Input: (batch, 64, 35) — 64 bars × 35 features
  │
  ▼
TCN Backbone (dilated causal convolutions)
  │
  ├──▶ Long Head (FC → sigmoid) → P(long win)
  │
  └──▶ Short Head (FC → sigmoid) → P(short win)
```

---

## Walk-Forward Validation (Shared)

BOTH tracks use identical walk-forward splits. No random CV. No leakage.

### Split Design

```
Walk-forward by SESSION BLOCKS (not by date):

Window 1: Train sessions 1-40  → Test sessions 41-45
Window 2: Train sessions 1-45  → Test sessions 46-50
Window 3: Train sessions 1-50  → Test sessions 51-55
...

Expanding window (not rolling) — each window trains on ALL prior data.
```

### Session = one trading day for equities, configurable for forex

For equities: 1 session = 1 trading day (9:30-16:00)
For forex: 1 session = 1 NY session (8:00 AM - 5:00 PM ET)
For crypto: 1 session = 1 UTC day

### Sequence model boundary rule

TCN sequences MUST NOT cross session boundaries. If a 64-bar lookback
would span two sessions, truncate or pad with zeros. This is critical
for preventing leakage in the sequence model.

---

## Evaluation Metrics (Same for Both Tracks)

### Primary Metrics (must be positive for deployment)

| Metric | What it measures | Target |
|---|---|---|
| IC (long) | Correlation between P(long) and actual forward return | > 0.03 |
| IC (short) | Correlation between P(short) and actual forward return (inverted) | > 0.03 |
| Long WR at P > 0.55 | Win rate for high-confidence long predictions | > 33.3% |
| Short WR at P > 0.55 | Win rate for high-confidence short predictions | > 33.3% |
| Spread (long) | Return of top decile - bottom decile (long model) | > 0 |
| Spread (short) | Same for short model | > 0 |

### Diagnostic Metrics (for comparison, not deployment gates)

| Metric | Notes |
|---|---|
| IC by walk-forward window | Stability check |
| IC by asset class | Does the model work on forex AND equities? |
| IC by session phase | Morning vs midday vs afternoon |
| Feature importance (LGBM) | Which features drive each side |
| Probability calibration | Is 0.60 really 60%? |
| Cost-adjusted spread | After estimated execution costs |
| Bucket breakdown | WR by probability bucket [0.50-0.55), [0.55-0.60), etc. |

---

## Training Compute

### Track A: LightGBM
- **Where:** Local Mac
- **Time:** 20 walk-forward windows × 60 sec each = ~20 min total
- **Memory:** < 4 GB
- **No GPU needed**

### Track B: TCN
- **Where:** Cloud GPU (Lambda $0.50/hr, Vast.ai $0.30/hr, or Colab)
- **Time:** ~1-4 hours depending on data size
- **Memory:** 8-16 GB GPU RAM
- **Cost:** ~$1-2 per training run

---

## Output Contract

### Model Artifacts

```
~/SS/Vanguard/models/
├── lgbm_long_v1/
│   ├── model.pkl
│   ├── features.json           # ordered feature list
│   ├── config.json             # hyperparams, TBM params
│   └── walkforward_results.json
├── lgbm_short_v1/
│   ├── model.pkl
│   ├── features.json
│   ├── config.json
│   └── walkforward_results.json
├── tcn_dual_v1/
│   ├── model.pt                # PyTorch checkpoint
│   ├── features.json
│   ├── config.json
│   └── walkforward_results.json
└── model_comparison.json       # Head-to-head metrics
```

### DB Table: vanguard_model_registry

```sql
CREATE TABLE IF NOT EXISTS vanguard_model_registry (
    model_id TEXT PRIMARY KEY,
    model_family TEXT NOT NULL,         -- lgbm_long, lgbm_short, tcn_dual
    track TEXT NOT NULL,                -- A (tabular) or B (sequence)
    target_label TEXT NOT NULL,         -- label_long, label_short, both
    feature_count INTEGER,
    train_sessions INTEGER,
    test_sessions INTEGER,
    walk_forward_windows INTEGER,
    mean_ic_long REAL,
    mean_ic_short REAL,
    mean_spread_long REAL,
    mean_spread_short REAL,
    long_wr_at_55 REAL,
    short_wr_at_55 REAL,
    artifact_path TEXT,
    created_at_utc TEXT,
    status TEXT DEFAULT 'trained',      -- trained, validated, deployed, retired
    notes TEXT
);
```

### DB Table: vanguard_walkforward_results

```sql
CREATE TABLE IF NOT EXISTS vanguard_walkforward_results (
    model_id TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    train_start TEXT,
    train_end TEXT,
    test_start TEXT,
    test_end TEXT,
    test_rows INTEGER,
    ic_long REAL,
    ic_short REAL,
    spread_long REAL,
    spread_short REAL,
    wr_long_55 REAL,
    wr_short_55 REAL,
    PRIMARY KEY (model_id, window_id)
);
```

---

## Model Comparison Report

After both tracks complete, V4B produces a head-to-head comparison:

```
=== Vanguard Model Comparison ===

                    LGBM Long   LGBM Short  TCN Dual (L)  TCN Dual (S)
Mean IC:            0.041       0.035       0.038         0.031
Spread:             0.32%       0.28%       0.35%         0.26%
WR at P>0.55:       41.2%       38.7%       39.5%         36.1%
Stability (IC std): 0.012       0.015       0.018         0.021
By equity:          0.043       0.037       0.040         0.033
By forex:           0.031       0.029       0.028         0.025

Recommendation: Deploy LGBM Long + LGBM Short (more stable, no GPU needed)
Challenger: TCN Dual shows promise on long side, monitor next retrain
```

Config-driven deployment decision — human picks which models go live.

---

## LightGBM Hyperparameters (Default)

```python
lgbm_params = {
    'objective': 'binary',
    'metric': 'auc',
    'boosting_type': 'gbdt',
    'num_leaves': 31,
    'learning_rate': 0.05,
    'feature_fraction': 0.8,
    'bagging_fraction': 0.8,
    'bagging_freq': 5,
    'min_child_samples': 50,
    'n_estimators': 500,
    'early_stopping_rounds': 50,
    'verbose': -1,
    'seed': 42,
}
```

### TCN Architecture (Default)

```python
tcn_config = {
    'input_size': 35,               # features
    'sequence_length': 64,          # bars lookback
    'num_channels': [64, 64, 32],   # dilated conv channels
    'kernel_size': 3,
    'dropout': 0.2,
    'dual_head': True,              # long + short outputs
    'learning_rate': 0.001,
    'batch_size': 256,
    'epochs': 100,
    'early_stopping_patience': 10,
}
```

---

## CLI

```bash
# Train both tracks
python3 stages/vanguard_model_trainer.py --track all

# LightGBM only (fast, no GPU)
python3 stages/vanguard_model_trainer.py --track A

# TCN only (needs GPU)
python3 stages/vanguard_model_trainer.py --track B

# Specific model
python3 stages/vanguard_model_trainer.py --track A --model lgbm_long

# Custom walk-forward
python3 stages/vanguard_model_trainer.py --train-sessions 30 --test-sessions 5

# Compare existing models
python3 stages/vanguard_model_trainer.py --compare-only

# Debug mode (verbose diagnostics)
python3 stages/vanguard_model_trainer.py --debug --track A
```

---

## File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_model_trainer.py       # V4B entry point
├── vanguard/
│   ├── models/
│   │   ├── lgbm_trainer.py             # LightGBM training logic
│   │   ├── tcn_trainer.py              # TCN training logic
│   │   ├── tcn_architecture.py         # TCN model definition (PyTorch)
│   │   ├── walkforward.py              # Shared walk-forward split logic
│   │   ├── evaluator.py                # Shared metrics computation
│   │   └── comparison.py               # Head-to-head comparison
│   └── __init__.py
├── models/                              # Saved artifacts
│   ├── lgbm_long_v1/
│   ├── lgbm_short_v1/
│   └── tcn_dual_v1/
├── config/
│   ├── vanguard_lgbm_params.json
│   └── vanguard_tcn_params.json
└── tests/
    └── test_vanguard_trainer.py
```

---

## Acceptance Criteria

- [ ] stages/vanguard_model_trainer.py exists and runs
- [ ] Track A: 2 × LightGBM (long + short) trains successfully
- [ ] Track B: 1 × TCN dual-head trains successfully
- [ ] Both tracks use identical walk-forward splits
- [ ] Walk-forward by session blocks, no random CV
- [ ] TCN sequences respect session boundaries (no leakage)
- [ ] All 35 features to all models (no manual subsetting)
- [ ] Evaluation metrics: IC, spread, WR by bucket, stability
- [ ] Feature importance saved for LightGBM models
- [ ] Model artifacts saved with config snapshots
- [ ] vanguard_model_registry updated
- [ ] vanguard_walkforward_results written
- [ ] Head-to-head comparison report generated
- [ ] Config-driven deployment decision (human picks winners)
- [ ] No imports from v2_*.py (Meridian)
- [ ] LightGBM runs on Mac without GPU
- [ ] TCN supports cloud GPU training

---

## Out of Scope

- Live inference / scoring (V5)
- Selection / ranking logic (V5)
- Agreement logic between models (V5)
- Risk filters (V6)
- Execution (V7)
- Feature subsetting per model (removed — all 35 to all models, let algorithms decide)
- Ensemble stacking / meta-learner (future Phase 3 if needed)
