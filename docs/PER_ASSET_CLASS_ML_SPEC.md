# PER-ASSET-CLASS ML MODELS — Architecture Spec
# Deviation from original V4B spec (which trained one model for all assets)
# Date: Mar 31, 2026
# For: GPT (business rules + QA pass) + Claude Code (implementation)

---

## Why This Change

The original V4B spec trains ONE lgbm_long + ONE lgbm_short on all 35 features
across all asset classes. This was designed when the system only had equity data.

Now that we have forex, commodities, indices, and crypto data flowing via
Twelve Data, a single model trained on mixed asset classes will perform poorly
because:

1. **Feature semantics differ across assets.** `rs_vs_benchmark_intraday`
   (relative strength vs SPY) is meaningful for equities, meaningless for
   EUR/USD. `session_opening_range_position` resets at 9:30 ET for equities
   but forex sessions overlap continuously.

2. **TBM parameters should differ.** `tp_pct=0.003` (0.30%) is reasonable
   for a $50 stock but too wide for EUR/USD (which moves ~0.05% in 30 min)
   and too tight for BTC/USD (which moves 1-2% easily).

3. **Volume semantics differ.** Equities have real volume. Forex has tick
   volume (not comparable). Crypto volume is often wash-traded.

4. **Session structure differs.** Equities: 6.5 hours. Forex: 24/5 with
   session overlaps. Crypto: 24/7.

---

## Proposed Architecture: Model Groups

```
V4B Model Training
│
├── EQUITY MODEL GROUP (trained on equity bars only)
│   ├── lgbm_equity_long    — 35 features, equity TBM params
│   ├── lgbm_equity_short   — 35 features, equity TBM params
│   └── Session: 9:30-16:00 ET, 78 bars/day
│
├── FOREX MODEL GROUP (trained on forex bars only)
│   ├── lgbm_forex_long     — ~25 features (drop equity-specific)
│   ├── lgbm_forex_short    — ~25 features
│   └── Session: 24/5, NY session 8:00-17:00 ET
│
├── COMMODITIES MODEL GROUP (metals + energy + agriculture)
│   ├── lgbm_commodity_long  — ~28 features
│   ├── lgbm_commodity_short — ~28 features
│   └── Session: varies by commodity
│
├── CRYPTO MODEL GROUP (trained on crypto bars only)
│   ├── lgbm_crypto_long    — ~25 features
│   ├── lgbm_crypto_short   — ~25 features
│   └── Session: 24/7, 1 UTC day
│
└── INDEX MODEL GROUP (trained on index bars only)
    ├── lgbm_index_long     — ~30 features
    ├── lgbm_index_short    — ~30 features
    └── Session: varies by index (most ~US hours)
```

---

## What Changes From Original V4B Spec

### V4A Changes (Training Backfill)

| Aspect | Original Spec | New Spec |
|---|---|---|
| TBM params | One set for all | Per-asset-class params |
| Session boundaries | Equity-centric | Per-asset-class sessions |
| Feature computation | All 35 for all | Feature subset per group |
| Training data table | One table, mixed | One table, asset_class column used for filtering |

The `vanguard_training_data` table already has an `asset_class` column.
No schema change needed — just filter by asset_class during training.

### Per-Asset-Class TBM Parameters

| Asset Class | tp_pct | sl_pct | horizon_bars | R:R | Notes |
|---|---|---|---|---|---|
| equity | 0.0030 | 0.0015 | 6 (30 min) | 2:1 | Original defaults |
| forex | 0.0010 | 0.0005 | 12 (60 min) | 2:1 | Tighter moves, longer horizon |
| commodity | 0.0025 | 0.00125 | 8 (40 min) | 2:1 | Between equity and forex |
| crypto | 0.0060 | 0.0030 | 6 (30 min) | 2:1 | High vol, wider barriers |
| index | 0.0020 | 0.0010 | 8 (40 min) | 2:1 | Less volatile than single stocks |

These are starting points. Walk-forward validation will prove which work.

### V4B Changes (Model Training)

| Aspect | Original Spec | New Spec |
|---|---|---|
| Models trained | 2 (lgbm_long, lgbm_short) | 10 (2 per asset class × 5 groups) |
| Feature sets | All 35 to all | Per-group subsets |
| Walk-forward splits | By session (one definition) | Per-group session definitions |
| Model registry | 2 rows | 10 rows |
| model_family naming | lgbm_long, lgbm_short | lgbm_equity_long, lgbm_forex_short, etc |

### Feature Subsets Per Group

**Equity (all 35):** Use all features. This is the primary lane.

**Forex (~25 features — drop 10):**
Drop: `gap_pct` (no gaps in 24/5), `session_opening_range_position` (unclear OR in forex),
`rs_vs_benchmark_intraday` (no SPY benchmark for forex), `daily_rs_vs_benchmark` (same),
`benchmark_momentum_12bar` (same), `daily_drawdown_from_high` (different meaning in 24/5),
`daily_conviction` (no daily equity system for forex), `bars_since_session_open` (ambiguous for 24/5),
`session_phase` (overlap sessions), `asset_class_encoded` (constant value for forex group).

**Commodities (~28 features — drop 7):**
Drop: `rs_vs_benchmark_intraday`, `daily_rs_vs_benchmark`, `benchmark_momentum_12bar`
(benchmark is DXY for gold, not SPY), `session_opening_range_position` (varies by commodity),
`gap_pct` (may or may not exist), `daily_conviction`, `asset_class_encoded`.
Keep: `cross_asset_correlation` (very important for gold/DXY inverse).

**Crypto (~25 features — drop 10):**
Drop: same as forex + `spread_proxy` (crypto spreads are different animal),
`session_phase` (no sessions), `time_in_session_pct` (24/7).

**Index (~30 features — drop 5):**
Drop: `daily_conviction`, `asset_class_encoded`, `rs_vs_benchmark_intraday`
(indices ARE the benchmark), `daily_rs_vs_benchmark`, `benchmark_momentum_12bar`.

### V5 Changes (Strategy Router)

V5 already routes strategies per asset class. The change is in how V5
reads model predictions:

```python
# Original: one model_id for all
probs = predict(model_id="lgbm_long", features=features_df)

# New: model_id per asset class
for asset_class in ["equity", "forex", "commodity", "crypto", "index"]:
    class_features = features_df[features_df["asset_class"] == asset_class]
    model_id = f"lgbm_{asset_class}_long"
    probs = predict(model_id=model_id, features=class_features)
```

---

## Implementation Order

1. **V4A: Add per-asset-class TBM params** to training backfill config
   - `config/vanguard_tbm_params.json` with per-class tp/sl/horizon
   - `vanguard_training_backfill_fast.py` reads params by asset_class

2. **V4A: Run training backfill** for non-equity asset classes
   - Filter existing bars by asset_class
   - Use per-class TBM params
   - Write to same `vanguard_training_data` table

3. **V4B: Train per-asset-class models**
   - Loop over asset classes
   - Filter training data by asset_class
   - Use per-class feature subsets
   - Write to model_registry with family like `lgbm_equity_long`

4. **V4B: Model comparison per asset class**
   - Walk-forward metrics per group
   - Deploy best per group

5. **V5: Update model loading in strategy router**
   - `predict_probabilities()` looks up model by asset_class
   - Fallback: if no per-class model exists, use the equity model

---

## What Does NOT Change

- V3 Factor Engine — still computes all 35 features for all assets. Feature
  subsetting happens at TRAINING time (V4B), not computation time (V3).
- V5 Strategy Router architecture — strategies are already per-asset-class.
- V6 Risk Filters — account profiles are prop-firm-specific, not asset-class-specific.
- V7 Orchestrator — runs V1→V6 per cycle, doesn't care about model internals.
- DB schema — `vanguard_training_data` already has `asset_class` column.
  `vanguard_model_registry` already has `model_family` column.

---

## Risk Assessment

**Low risk:** Feature subsetting and per-class TBM params are config changes,
not architecture changes. The training pipeline already handles asset_class.

**Medium risk:** Per-class models need sufficient training data to be reliable.
With 1 year of historical backfill from both Alpaca (equities) and Twelve Data
(forex/commodities/crypto/indices), we should have enough:

| Asset Class | Source | ~Bars/Day | 1yr Estimate | Training Rows |
|---|---|---|---|---|
| Equity (2,500 syms) | Alpaca free REST | 78/sym | ~48M 5m bars | Massive |
| Forex (28 pairs) | Twelve Data Grow | 288/sym | ~2.9M 5m bars | ~80K/pair |
| Metals (5 syms) | Twelve Data Grow | 288/sym | ~525K 5m bars | ~105K/sym |
| Crypto (15 syms) | Twelve Data Grow | 288/sym | ~1.6M 5m bars | ~105K/sym |
| Index (11 syms) | Twelve Data Grow | ~78/sym | ~312K 5m bars | ~28K/sym |

This is more than enough for robust walk-forward validation per asset class.

**Backfill plan:**
- Alpaca equity historical: `--days 365 --max-symbols 2500` (~15-30 min)
- Twelve Data non-equity: `--days 365` (~85 min at 55 API/min)
- Both run tonight as nohup jobs
- V4A training backfill runs on the full dataset tomorrow morning

**Mitigation:** Still start with equity model as default fallback.
Train per-class models on the backfilled data. Deploy per-class only
when walk-forward proves they outperform the generic model.

---

## For GPT (Business Rules / QA Pass)

Questions GPT should address:

1. Should the per-class models be behind a feature flag? (Default: yes,
   start with equity model for all, enable per-class when validated)

2. How should the UI expose per-class model quality? (Model comparison
   page per asset class?)

3. Should V5 strategy scoring weights differ when using per-class models
   vs the generic model? (The ML gate threshold might need tuning per class)

4. How does this affect Combined mode in the UI? (Different models behind
   different rows in the same table — the provenance field should reflect
   which model scored each row)

5. What's the minimum training data threshold before deploying a per-class
   model? (Recommendation: 20+ sessions, 5,000+ training rows minimum)
