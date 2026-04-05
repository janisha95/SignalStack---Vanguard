# CODEX — Apply ML Improvements to Existing Vanguard Trainer

## READ FIRST — DO NOT REPLACE FILES

The current `vanguard_model_trainer.py` has been patched multiple times:
- OOM fix: SQL-level asset_class filtering
- Feature profiles: per-asset curated feature lists from config
- --asset-class and --max-rows CLI flags
- Progress logging with flush=True

**DO NOT replace the file.** Apply the changes below SURGICALLY to the existing code.

```bash
# 1. Current state of the file
wc -l ~/SS/Vanguard/stages/vanguard_model_trainer.py
head -50 ~/SS/Vanguard/stages/vanguard_model_trainer.py
grep -n "def " ~/SS/Vanguard/stages/vanguard_model_trainer.py

# 2. Current split method
grep -n "split\|TRAIN_FRAC\|0\.70\|70\|30\|walk_forward\|temporal" ~/SS/Vanguard/stages/vanguard_model_trainer.py

# 3. Current training function signature
grep -n "def train\|def fit\|def run_training\|lgbm.*fit\|model\.fit" ~/SS/Vanguard/stages/vanguard_model_trainer.py

# 4. What columns exist in training data
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_training_data)" | head -50
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT DISTINCT asset_class FROM vanguard_training_data"

# 5. Does session_id or forward_return exist?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN forward_return IS NOT NULL THEN 1 ELSE 0 END) as has_fwd_return,
  COUNT(DISTINCT date(asof_ts_utc)) as distinct_dates
FROM vanguard_training_data
WHERE asset_class = 'forex'
LIMIT 1
"
```

**Report ALL output before making changes.**

---

## CHANGE 1: Replace 70/30 Split with Purged Walk-Forward

Find the current split logic (likely a single 70/30 temporal split). Replace with:

```python
def purged_walk_forward_split(df: pd.DataFrame, n_splits: int = 5, embargo_bars: int = 6):
    """
    Purged walk-forward by date blocks.
    Expanding window: each fold trains on ALL prior data.
    Embargo: skip `embargo_bars` rows between train and test to prevent leakage.
    """
    df = df.sort_values("asof_ts_utc").reset_index(drop=True)
    
    # Group by date (approximate sessions)
    dates = pd.to_datetime(df["asof_ts_utc"]).dt.date.unique()
    dates = sorted(dates)
    
    # Minimum 60% of dates for first training window
    min_train = int(len(dates) * 0.6)
    test_size = max(1, (len(dates) - min_train) // n_splits)
    
    for i in range(n_splits):
        train_end_idx = min_train + i * test_size
        test_start_idx = train_end_idx
        test_end_idx = min(test_start_idx + test_size, len(dates))
        
        if test_end_idx > len(dates):
            break
            
        train_dates = set(dates[:train_end_idx])
        test_dates = set(dates[test_start_idx:test_end_idx])
        
        train_mask = pd.to_datetime(df["asof_ts_utc"]).dt.date.isin(train_dates)
        test_mask = pd.to_datetime(df["asof_ts_utc"]).dt.date.isin(test_dates)
        
        # Purge: remove last `embargo_bars` rows from training set
        if embargo_bars > 0:
            train_indices = df[train_mask].index
            if len(train_indices) > embargo_bars:
                purge_indices = train_indices[-embargo_bars:]
                train_mask.iloc[purge_indices] = False
        
        if train_mask.sum() < 100 or test_mask.sum() < 50:
            continue
            
        yield train_mask, test_mask
```

Replace the existing split call:
```python
# OLD (find and replace):
# train_df, test_df = temporal_split(df, 0.70)  # or similar

# NEW:
ics_per_window = []
for fold_i, (train_mask, test_mask) in enumerate(purged_walk_forward_split(df, n_splits=5, embargo_bars=6)):
    print(f"[V4B] Walk-forward window {fold_i+1}: train={train_mask.sum():,} test={test_mask.sum():,}", flush=True)
    
    X_train = df.loc[train_mask, feature_cols].values
    y_train = df.loc[train_mask, label_col].values
    X_test = df.loc[test_mask, feature_cols].values
    y_test = df.loc[test_mask, label_col].values
    
    # Sample weights (concurrency-based)
    weights = compute_sample_weights(df.loc[train_mask])
    
    model = lgb.LGBMClassifier(**lgbm_params)
    model.fit(
        X_train, y_train,
        sample_weight=weights,
        eval_set=[(X_test, y_test)],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
    )
    
    proba = model.predict_proba(X_test)[:, 1]
    
    # IC vs return (if forward_return exists)
    if "forward_return" in df.columns:
        fwd = df.loc[test_mask, "forward_return"].values
        ic = spearmanr(proba, fwd).statistic
    else:
        ic = spearmanr(proba, y_test).statistic
    
    ics_per_window.append(ic)
    print(f"[V4B]   Window {fold_i+1} IC: {ic:.4f}", flush=True)

mean_ic = np.mean(ics_per_window)
print(f"[V4B] Mean IC across {len(ics_per_window)} windows: {mean_ic:.4f}", flush=True)
```

---

## CHANGE 2: Add Sample Weights (Concurrency-Based)

Add this function near the top of the file:

```python
def compute_sample_weights(df: pd.DataFrame) -> np.ndarray:
    """
    Label uniqueness weighting.
    Rows at timestamps with more overlapping samples get lower weight.
    Prevents the model from memorizing busy periods.
    """
    if "asof_ts_utc" not in df.columns:
        return np.ones(len(df))
    concurrency = df.groupby("asof_ts_utc").size()
    weights = 1.0 / df["asof_ts_utc"].map(concurrency).values
    # Normalize so mean weight = 1
    weights = weights / weights.mean()
    return weights
```

---

## CHANGE 3: Cross-Sectional Normalization (in factor engine OR pre-training)

Add this normalization step BEFORE training:

```python
def cross_sectional_normalize(df: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    """
    Z-score features across all assets at the SAME timestamp.
    Critical for multi-asset models where feature scales differ.
    """
    df = df.copy()
    # Group by timestamp and z-score each feature
    for col in feature_cols:
        if col in df.columns:
            df[col] = df.groupby("asof_ts_utc")[col].transform(
                lambda x: (x - x.mean()) / (x.std() + 1e-8)
            )
    return df
```

Apply BEFORE feature extraction in the training loop:
```python
# After loading data, before training:
df = cross_sectional_normalize(df, feature_cols)
```

---

## CHANGE 4: Log IC Per Window + Aggregate Metrics

At the end of training (after the walk-forward loop), print a summary:

```python
print(f"\n{'='*60}", flush=True)
print(f"  {model_id} — Walk-Forward Summary", flush=True)
print(f"{'='*60}", flush=True)
print(f"  Windows         : {len(ics_per_window)}", flush=True)
print(f"  Mean IC         : {mean_ic:.4f}", flush=True)
print(f"  IC Std Dev      : {np.std(ics_per_window):.4f}", flush=True)
print(f"  IC Hit Rate     : {sum(1 for ic in ics_per_window if ic > 0) / len(ics_per_window):.1%}", flush=True)
print(f"  Best Window IC  : {max(ics_per_window):.4f}", flush=True)
print(f"  Worst Window IC : {min(ics_per_window):.4f}", flush=True)
print(f"{'='*60}\n", flush=True)
```

---

## DO NOT CHANGE

- CLI argument handling (--asset-class, --max-rows)
- SQL-level asset_class filtering (OOM fix)
- Feature profile loading from config
- Model saving logic
- Progress logging infrastructure

---

## VERIFY

```bash
# Train forex with new purged CV:
cd ~/SS/Vanguard && python3 -u stages/vanguard_model_trainer.py --asset-class forex 2>&1 | tee logs/v4b_forex_purged.log

# Check for walk-forward output:
grep "Walk-forward\|Window.*IC\|Mean IC" logs/v4b_forex_purged.log
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: purged walk-forward CV + sample weights + cross-sectional norm in V4B trainer"
```
