# CODEX — Apply Model Fixes + IBKR Tuning (4 Changes)

## Context
Grok diagnosed the Vanguard prediction issue: train-serving skew + no cross-sectional
normalization on raw regressor outputs. Two patches fix this.
Also: IBKR symbol limit is 50 (should be 2000), and V1 runs all 3 adapters redundantly.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-grok-patches"
```

---

## CHANGE 1: V4B Scorer — Feature Standardization Before Inference

File: `~/SS/Vanguard/stages/vanguard_scorer.py`

The models were trained on features with certain distributions. Live V3 features
may have different distributions (train-serving skew). Standardizing features
cross-sectionally before predict() makes the input statistically similar to training.

Find where the feature matrix X is built and predict() is called.
ADD cross-sectional standardization BEFORE predict():

```python
# AFTER building X (the feature matrix) and BEFORE calling model.predict(X):

# Cross-sectional standardization (critical for train-serving alignment)
for i in range(X.shape[1]):
    col = X[:, i]
    mean = col.mean()
    std = col.std()
    if std > 1e-8:
        X[:, i] = (col - mean) / std
    else:
        X[:, i] = 0.0
```

If X is a DataFrame instead of numpy array:
```python
for col in X.columns:
    mean = X[col].mean()
    std = X[col].std()
    if std > 1e-8:
        X[col] = (X[col] - mean) / std
    else:
        X[col] = 0.0
```

Read the actual code first to see which format X is in:
```bash
grep -n "predict\|X\[.*\]\|feature.*matrix\|np\.zeros\|DataFrame" \
  ~/SS/Vanguard/stages/vanguard_scorer.py | head -20
```

---

## CHANGE 2: V5 Selection — Cross-Sectional Percentile Ranking

File: `~/SS/Vanguard/stages/vanguard_selection_simple.py`

Raw predicted_return values are tiny floats (±0.00005). Replace direct value
comparison with cross-sectional percentile ranking.

Find the selection/ranking logic and replace it:

```python
# REPLACE the existing selection logic with:

# Cross-sectional percentile rank per cycle (0.0 to 1.0)
frame = preds.copy()
frame['edge_score'] = frame.groupby('cycle_ts_utc')['predicted_return'].rank(pct=True)

# Gentle lift to avoid zero collapse
frame['edge_score'] = frame['edge_score'] * 0.98 + 0.01

# Direction from raw prediction sign (preserves model intent)
frame['direction'] = np.where(frame['predicted_return'] > 0, 'LONG', 'SHORT')

# Debug logging
logger.info(
    "[V5] edge_score range: %.4f–%.4f | LONG%% = %.1f%%",
    frame['edge_score'].min(), frame['edge_score'].max(),
    (frame['direction'] == 'LONG').mean() * 100
)

# Select top N per direction per asset class
selected = []
for asset_class, grp in frame.groupby('asset_class'):
    longs = grp[grp['direction'] == 'LONG'].nlargest(top_n, 'edge_score').copy()
    shorts = grp[grp['direction'] == 'SHORT'].nsmallest(top_n, 'edge_score').copy()
    # For shorts, we want lowest edge_score (most bearish)
    # Actually: shorts should be ranked by most negative predicted_return
    shorts = grp[grp['direction'] == 'SHORT'].nsmallest(top_n, 'predicted_return').copy()
    
    if not longs.empty:
        longs['rank'] = range(1, len(longs) + 1)
        selected.append(longs)
    if not shorts.empty:
        shorts['rank'] = range(1, len(shorts) + 1)
        selected.append(shorts)
```

Read the actual current code first:
```bash
cat ~/SS/Vanguard/stages/vanguard_selection_simple.py
```

---

## CHANGE 3: Raise IBKR Symbol Limit from 50 to 2000

File: `~/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py`

Line 60: `IB_INTRADAY_SYMBOL_LIMIT = int(os.environ.get("IB_INTRADAY_SYMBOL_LIMIT", "50"))`

Change default from "50" to "2000":
```python
IB_INTRADAY_SYMBOL_LIMIT = int(os.environ.get("IB_INTRADAY_SYMBOL_LIMIT", "2000"))
```

BUT: IBKR pacing rules limit to ~60 historical data requests per 10 minutes.
For 2000 symbols, the adapter must use streaming (keepUpToDate=True) instead of
individual reqHistoricalData calls.

Check how the adapter currently fetches bars:
```bash
grep -n "reqHistoricalData\|keepUpToDate\|reqRealTimeBars\|reqMktData\|pacing\|batch\|sleep" \
  ~/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py | head -20
```

If it's making individual reqHistoricalData calls per symbol, it CANNOT scale to
2000 symbols without hitting pacing limits. The fix is:
- For streaming: use reqMktData for top-of-book quotes, aggregate into bars locally
- For historical: batch in groups of 50, sleep 10s between batches
- For the prefilter: use reqScannerSubscription to get liquid equities efficiently

If scaling is too complex right now, set a reasonable limit like 200-500 and
batch with sleep:

```python
IB_INTRADAY_SYMBOL_LIMIT = int(os.environ.get("IB_INTRADAY_SYMBOL_LIMIT", "500"))
```

---

## CHANGE 4: Disable Redundant Twelve Data Forex Polling

File: `~/SS/Vanguard/stages/vanguard_orchestrator.py`

V1 now runs ALL THREE adapters: Alpaca (3s) + Twelve Data (62s) + IBKR (new).
Since IBKR now handles forex, Twelve Data should only poll crypto.

Find where Twelve Data is called in V1:
```bash
grep -n "twelve\|td_adapter\|poll.*bars\|non.equity" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

Option A: Reduce Twelve Data to crypto-only symbols:
```python
# In the Twelve Data polling step, filter to crypto only:
td_symbols = [s for s in td_symbols if s.get('asset_class') == 'crypto']
```

Option B: Skip Twelve Data entirely if IBKR is connected:
```python
if self.ibkr_adapter and self.ibkr_adapter.is_connected():
    logger.info("[V1] IBKR connected — skipping Twelve Data forex")
    # Only poll crypto via Twelve Data
    td_symbols = [s for s in td_symbols if asset_class_for(s) == 'crypto']
```

This should drop V1 from 103s to ~10-15s.

---

## Verification

```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -40

# Check V5 edge_score debug output
# Should see: [V5] edge_score range: 0.0100–0.9900 | LONG% = XX.X%

# Check prediction differentiation
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, COUNT(*),
    ROUND(AVG(predicted_return), 8) as avg_pred,
    ROUND(MIN(predicted_return), 8) as min_pred,
    ROUND(MAX(predicted_return), 8) as max_pred
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class, direction
"

# Check stage timings (should be much faster without TD forex)
# Look for: v1: Xs (was 103s)
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: cross-sectional ranking + feature standardization + IBKR tuning"
```
