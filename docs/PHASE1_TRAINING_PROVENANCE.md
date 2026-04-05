# PHASE 1: Per-Asset-Class Training + Provenance
# V4A TBM params + V4B model training per asset class + model registry metadata
# Give to Codex FIRST — this is the foundation
# Date: Mar 31, 2026

---

## SCOPE

Phase 1 adds:
1. Per-asset-class TBM parameters in V4A config
2. Per-asset-class model training in V4B (10 models instead of 2)
3. Model registry with full provenance metadata
4. Feature profile definitions per asset class

Phase 1 does NOT change V5 routing or V6 risk gates — those are Phase 2 and 3.

---

## READ FIRST

```bash
# Current model registry
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT * FROM vanguard_model_registry"

# Current training data by asset class
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) as rows, AVG(label_long) as long_rate, AVG(label_short) as short_rate
FROM vanguard_training_data GROUP BY asset_class
"

# Current V4B trainer
head -50 ~/SS/Vanguard/stages/vanguard_model_trainer.py

# Current V4A backfill (fast version)
head -50 ~/SS/Vanguard/stages/vanguard_training_backfill_fast.py
```

---

## 1. TBM PARAMETER CONFIG

Create `config/vanguard_tbm_params.json`:

```json
{
    "equity": {
        "tp_pct": 0.0030,
        "sl_pct": 0.0015,
        "horizon_bars": 6,
        "rr_ratio": 2.0,
        "session_end": "16:00",
        "session_tz": "America/New_York",
        "version": "tbm_equity_v1"
    },
    "forex": {
        "tp_pct": 0.0010,
        "sl_pct": 0.0005,
        "horizon_bars": 12,
        "rr_ratio": 2.0,
        "session_end": "17:00",
        "session_tz": "America/New_York",
        "version": "tbm_forex_v1"
    },
    "commodity": {
        "tp_pct": 0.0025,
        "sl_pct": 0.00125,
        "horizon_bars": 8,
        "rr_ratio": 2.0,
        "session_end": "17:00",
        "session_tz": "America/New_York",
        "version": "tbm_commodity_v1"
    },
    "crypto": {
        "tp_pct": 0.0060,
        "sl_pct": 0.0030,
        "horizon_bars": 6,
        "rr_ratio": 2.0,
        "session_end": null,
        "session_tz": "UTC",
        "version": "tbm_crypto_v1"
    },
    "index": {
        "tp_pct": 0.0020,
        "sl_pct": 0.0010,
        "horizon_bars": 8,
        "rr_ratio": 2.0,
        "session_end": "16:00",
        "session_tz": "America/New_York",
        "version": "tbm_index_v1"
    }
}
```

V4A training backfill reads this config and applies the correct TBM params per asset_class.

---

## 2. FEATURE PROFILES

Create `config/vanguard_feature_profiles.json`:

```json
{
    "equity": {
        "profile": "all_features_v1",
        "features": "all_35",
        "drop": []
    },
    "forex": {
        "profile": "fx_features_v1",
        "features": "subset_25",
        "drop": [
            "gap_pct", "session_opening_range_position",
            "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark",
            "benchmark_momentum_12bar", "daily_drawdown_from_high",
            "daily_conviction", "bars_since_session_open",
            "session_phase", "asset_class_encoded"
        ]
    },
    "commodity": {
        "profile": "commodity_features_v1",
        "features": "subset_28",
        "drop": [
            "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark",
            "benchmark_momentum_12bar", "session_opening_range_position",
            "gap_pct", "daily_conviction", "asset_class_encoded"
        ]
    },
    "crypto": {
        "profile": "crypto_features_v1",
        "features": "subset_25",
        "drop": [
            "gap_pct", "session_opening_range_position",
            "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark",
            "benchmark_momentum_12bar", "daily_drawdown_from_high",
            "daily_conviction", "bars_since_session_open",
            "session_phase", "asset_class_encoded"
        ]
    },
    "index": {
        "profile": "index_features_v1",
        "features": "subset_30",
        "drop": [
            "daily_conviction", "asset_class_encoded",
            "rs_vs_benchmark_intraday", "daily_rs_vs_benchmark",
            "benchmark_momentum_12bar"
        ]
    }
}
```

---

## 3. V4B MODEL TRAINING — PER ASSET CLASS

Update `stages/vanguard_model_trainer.py`:

```python
# The training loop changes from:
#   train lgbm_long on ALL data
#   train lgbm_short on ALL data
#
# To:
#   for each asset_class in [equity, forex, commodity, crypto, index]:
#       filter training_data by asset_class
#       load feature profile (drop columns per config)
#       load TBM params (for logging/metadata)
#       if rows >= MIN_TRAINING_ROWS (5000):
#           train lgbm_{asset_class}_long
#           train lgbm_{asset_class}_short
#           write to model_registry with full metadata
#       else:
#           log "insufficient data for {asset_class}, skip"

MIN_TRAINING_ROWS = 5000
MIN_VALIDATION_WINDOWS = 3
```

### Model naming convention:
```
lgbm_equity_long_v1
lgbm_equity_short_v1
lgbm_forex_long_v1
lgbm_forex_short_v1
lgbm_commodity_long_v1
lgbm_commodity_short_v1
lgbm_crypto_long_v1
lgbm_crypto_short_v1
lgbm_index_long_v1
lgbm_index_short_v1
```

### Model registry — add columns if not present:
```sql
ALTER TABLE vanguard_model_registry ADD COLUMN asset_class TEXT;
ALTER TABLE vanguard_model_registry ADD COLUMN direction TEXT;
ALTER TABLE vanguard_model_registry ADD COLUMN feature_profile TEXT;
ALTER TABLE vanguard_model_registry ADD COLUMN tbm_profile TEXT;
ALTER TABLE vanguard_model_registry ADD COLUMN training_rows INTEGER;
ALTER TABLE vanguard_model_registry ADD COLUMN readiness TEXT DEFAULT 'not_trained';
ALTER TABLE vanguard_model_registry ADD COLUMN fallback_family TEXT;
```

### Readiness states (from GPT QA review):
- `not_trained` — no model artifact
- `trained_insufficient_validation` — trained but below promotion standard
- `validated_shadow` — validated, shadow tracking only
- `live_native` — approved for live scoring
- `live_fallback` — native unavailable, equity fallback used
- `disabled` — explicitly blocked

### Walk-forward results — add asset_class:
```sql
ALTER TABLE vanguard_walkforward_results ADD COLUMN asset_class TEXT;
```

---

## 4. V4A UPDATE — PER-ASSET TBM IN BACKFILL

Update `stages/vanguard_training_backfill_fast.py` to:
1. Read `config/vanguard_tbm_params.json`
2. For each symbol, look up its `asset_class`
3. Apply the correct TBM params (tp_pct, sl_pct, horizon_bars)
4. Write `tbm_profile` column to `vanguard_training_data`

Add column if not present:
```sql
ALTER TABLE vanguard_training_data ADD COLUMN tbm_profile TEXT;
```

---

## VERIFY

```bash
# After V4A re-run with per-class TBM
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, tbm_profile, COUNT(*), AVG(label_long), AVG(label_short)
FROM vanguard_training_data GROUP BY asset_class, tbm_profile
"

# After V4B per-class training
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT model_id, asset_class, direction, readiness, training_rows, mean_ic_long, mean_ic_short
FROM vanguard_model_registry ORDER BY asset_class, direction
"
```
