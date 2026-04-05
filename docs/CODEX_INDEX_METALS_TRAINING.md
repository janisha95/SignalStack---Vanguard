# CODEX — Train LGBM Models for Index + Metals/Energy

## Context
Vanguard now produces equity predictions (IC +0.034) and forex predictions (IC +0.049).
We need models for index CFDs (US500.cash, US100.cash) and metals/energy (XAUUSD, USOIL).
These are needed for GFT prop firm accounts (5K + 10K) that trade CFDs.

CRITICAL: Training and inference must use the SAME feature computation.
The current train-serving skew issue (V4A backfill vs V3 live) must be avoided.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-index-metals-training"
```

---

## Step 0: Check what training data exists

```bash
# What asset classes have training data?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols,
    MIN(date) as earliest, MAX(date) as latest
FROM vanguard_training_data
GROUP BY asset_class
"

# What features does V3 compute for non-equity?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) as rows
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
GROUP BY asset_class
"

# What historical 5m bars exist for metals/index/energy?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, data_source, COUNT(*) as bars, COUNT(DISTINCT symbol) as symbols,
    MIN(bar_ts_utc) as earliest, MAX(bar_ts_utc) as latest
FROM vanguard_bars_5m
WHERE asset_class IN ('metal', 'energy', 'index')
GROUP BY asset_class, data_source
"

# What about from ibkr_intraday.db?
sqlite3 ~/SS/Vanguard/data/ibkr_intraday.db "
SELECT asset_class, COUNT(*) as bars, COUNT(DISTINCT symbol) as symbols
FROM ibkr_bars_5m
WHERE asset_class IN ('metal', 'energy', 'index')
GROUP BY asset_class
" 2>/dev/null

# Check what V3 features look like for latest crypto/forex (similar asset classes)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT *
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
AND asset_class = 'forex'
LIMIT 1
"
```

Report all output before proceeding.

---

## Step 1: Generate Training Data Using V3 Feature Engine (NOT V4A)

THIS IS CRITICAL: To avoid train-serving skew, generate training features using
the SAME code path that V3 uses in production.

### Option A: Replay V3 on historical bars (correct approach)

```python
"""
Replay V3 factor engine on historical 5m bars to generate training data.
This ensures training features match production features exactly.
"""
import sqlite3, pandas as pd, numpy as np
from pathlib import Path
from datetime import datetime, timedelta

DB = str(Path.home() / 'SS/Vanguard/data/vanguard_universe.db')

# Import V3 factor engine's compute function
# THIS is the key — use the same code as production
from stages.vanguard_factor_engine import compute_features_for_symbol

def generate_training_data(asset_classes=['metal', 'energy', 'index'],
                          forward_bars=6):  # 6 bars × 5m = 30 min ahead
    """
    For each asset class:
    1. Load all historical 5m bars
    2. For each bar timestamp, compute V3 features (same as production)
    3. Compute forward_return = (close[t+6] - close[t]) / close[t]
    4. Save to training table
    """
    con = sqlite3.connect(DB)
    
    for ac in asset_classes:
        # Get all symbols for this asset class
        symbols = [r[0] for r in con.execute("""
            SELECT DISTINCT symbol FROM vanguard_bars_5m
            WHERE asset_class = ? AND bar_ts_utc > datetime('now', '-30 days')
        """, (ac,)).fetchall()]
        
        print(f"\n=== {ac}: {len(symbols)} symbols ===")
        
        for sym in symbols:
            # Load historical 5m bars
            bars = pd.read_sql("""
                SELECT * FROM vanguard_bars_5m
                WHERE symbol = ? AND asset_class = ?
                ORDER BY bar_ts_utc
            """, con, params=(sym, ac))
            
            if len(bars) < forward_bars + 20:
                print(f"  {sym}: only {len(bars)} bars, skipping")
                continue
            
            # Compute forward return
            bars['forward_return'] = bars['close'].shift(-forward_bars) / bars['close'] - 1
            bars = bars.dropna(subset=['forward_return'])
            
            # Compute V3 features for each bar
            # This is the expensive part — calls the same function V3 uses
            features_list = []
            for idx, row in bars.iterrows():
                try:
                    features = compute_features_for_symbol(
                        sym, ac, row['bar_ts_utc'], con
                    )
                    features['forward_return'] = row['forward_return']
                    features['symbol'] = sym
                    features['asset_class'] = ac
                    features['bar_ts_utc'] = row['bar_ts_utc']
                    features_list.append(features)
                except Exception as e:
                    continue
            
            if features_list:
                df = pd.DataFrame(features_list)
                # Write to training table
                df.to_sql('vanguard_training_data', con, if_exists='append', index=False)
                print(f"  {sym}: {len(df)} training rows")
    
    con.close()
```

### Option B: If V3 compute is too slow for historical replay

Use the LATEST V3 features from vanguard_factor_matrix as a template,
and generate synthetic training data by replaying on historical bars
with a simplified feature computation that matches V3's output format.

Check how long V3 takes per symbol:
```bash
time python3 -c "
import sys; sys.path.insert(0, '.')
from stages.vanguard_factor_engine import run as run_v3
# Time V3 for just forex/metal/energy symbols
import sqlite3
con = sqlite3.connect('data/vanguard_universe.db')
# Get a small sample of symbols
symbols = con.execute(\"\"\"
    SELECT DISTINCT symbol FROM vanguard_health 
    WHERE asset_class IN ('metal', 'energy') AND status = 'ACTIVE'
    LIMIT 5
\"\"\").fetchall()
print(f'Testing V3 on {len(symbols)} symbols')
con.close()
"
```

---

## Step 2: Train LGBM Regressors

Once training data exists, train one model per asset class group:
- **metals_energy**: Combined model for XAUUSD, XAGUSD, USOIL, NATGAS, etc.
- **index**: Combined model for US500, US100, US30, etc.

### Training script:

```python
import pandas as pd, numpy as np, lightgbm as lgb, joblib, json
from sklearn.model_selection import TimeSeriesSplit
from scipy.stats import spearmanr
from pathlib import Path

DB = str(Path.home() / 'SS/Vanguard/data/vanguard_universe.db')
MODELS_DIR = Path.home() / 'SS/Vanguard/models'

def train_regressor(asset_classes, model_name, features=None):
    """
    Train LGBM regressor with walk-forward validation.
    Uses cross-sectional standardization (matching production).
    """
    import sqlite3
    con = sqlite3.connect(DB)
    
    # Load training data
    ac_filter = ','.join(f"'{ac}'" for ac in asset_classes)
    df = pd.read_sql(f"""
        SELECT * FROM vanguard_training_data
        WHERE asset_class IN ({ac_filter})
        ORDER BY bar_ts_utc
    """, con)
    con.close()
    
    print(f"Training data: {len(df)} rows, {df['symbol'].nunique()} symbols")
    
    if features is None:
        # Use same features as forex model (non-equity features)
        meta_cols = {'symbol', 'asset_class', 'bar_ts_utc', 'forward_return',
                     'cycle_ts_utc', 'date', 'created_at'}
        features = [c for c in df.columns if c not in meta_cols
                    and df[c].dtype in ['float64', 'float32', 'int64']
                    and df[c].notna().sum() > len(df) * 0.3]
    
    print(f"Features: {len(features)}")
    
    # Walk-forward validation
    X = df[features].fillna(0).values
    y = df['forward_return'].values
    
    tscv = TimeSeriesSplit(n_splits=5)
    ics = []
    
    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        
        # Cross-sectional standardization (match production V4B)
        for i in range(X_train.shape[1]):
            mean, std = X_train[:, i].mean(), X_train[:, i].std() + 1e-8
            X_train[:, i] = (X_train[:, i] - mean) / std
            X_test[:, i] = (X_test[:, i] - mean) / std
        
        model = lgb.LGBMRegressor(
            n_estimators=500, learning_rate=0.05, num_leaves=31,
            feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=5,
            min_child_samples=20, verbose=-1, random_state=42
        )
        model.fit(X_train, y_train)
        pred = model.predict(X_test)
        ic = spearmanr(pred, y_test).statistic
        ics.append(ic)
        print(f"  Fold {fold+1}: IC = {ic:+.4f}")
    
    avg_ic = np.mean(ics)
    print(f"\n  Average IC: {avg_ic:+.4f}")
    
    if avg_ic < 0.01:
        print(f"  WARNING: IC too low ({avg_ic:.4f}). Model may not have signal.")
    
    # Train final model on all data
    for i in range(X.shape[1]):
        mean, std = X[:, i].mean(), X[:, i].std() + 1e-8
        X[:, i] = (X[:, i] - mean) / std
    
    final_model = lgb.LGBMRegressor(
        n_estimators=500, learning_rate=0.05, num_leaves=31,
        feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=5,
        min_child_samples=20, verbose=-1, random_state=42
    )
    final_model.fit(X, y)
    
    # Save
    out_dir = MODELS_DIR / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(final_model, out_dir / f'{model_name}.pkl')
    json.dump({
        'model': model_name,
        'features': features,
        'asset_classes': asset_classes,
        'avg_ic': round(avg_ic, 4),
        'rows': len(df),
        'symbols': df['symbol'].nunique(),
        'trained_at': pd.Timestamp.now(tz='UTC').isoformat()
    }, open(out_dir / 'meta.json', 'w'), indent=2)
    
    print(f"\n  Saved to {out_dir}")
    return avg_ic

# Train metals + energy combined model
train_regressor(['metal', 'energy'], 'lgbm_metals_energy_v1')

# Train index model
train_regressor(['index'], 'lgbm_index_v1')
```

---

## Step 3: Register models in V4B scorer

After training, add the new models to `vanguard_scorer.py`:

```bash
grep -n "ASSET_CONFIGS\|MODEL_SPECS\|model_dir\|asset_class" \
  ~/SS/Vanguard/stages/vanguard_scorer.py | head -20
```

Add entries for metals_energy and index:

```python
# In MODEL_SPECS or ASSET_CONFIGS:
"metal": {
    "model_dir": "models/lgbm_metals_energy_v1",
    "models": {"lgbm": {"file": "lgbm_metals_energy_v1.pkl", "weight": 1.0}},
    "model_id": "lgbm_metals_energy_v1",
},
"energy": {
    "model_dir": "models/lgbm_metals_energy_v1",  # same model as metal
    "models": {"lgbm": {"file": "lgbm_metals_energy_v1.pkl", "weight": 1.0}},
    "model_id": "lgbm_metals_energy_v1",
},
"index": {
    "model_dir": "models/lgbm_index_v1",
    "models": {"lgbm": {"file": "lgbm_index_v1.pkl", "weight": 1.0}},
    "model_id": "lgbm_index_v1",
},
```

---

## Step 4: Verify end-to-end

```bash
# Run single cycle
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -20

# Check predictions now include all asset classes
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, COUNT(*)
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class, direction
"
# Expected: equity, forex, metal, energy, index all present

# Check shortlist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, direction, ROUND(predicted_return, 8) as pred, rank
FROM vanguard_shortlist_v2
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist_v2)
AND asset_class IN ('metal', 'energy', 'index')
ORDER BY asset_class, direction, rank
"
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: LGBM models for metals/energy + index asset classes"
```
