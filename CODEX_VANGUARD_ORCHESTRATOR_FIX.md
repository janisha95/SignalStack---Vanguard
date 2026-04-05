# CODEX — Fix Vanguard Orchestrator: Make It Actually Work

## Context
The Vanguard orchestrator ran ONE cycle but produced garbage:
- 35 forex predictions, ALL SHORT, near-identical values (-0.00005 range)
- 7/35 features are NaN for all forex symbols (20% NaN rate)
- Twelve Data API key fails intermittently
- V5 threshold was blocking everything (fixed manually, needs commit)
- Orchestrator needs to loop every 5 minutes, not single-cycle

The models ARE loaded and producing output. But the features feeding them are 
20% NaN, which means the models get zeros for critical features, producing 
near-constant predictions. Fix the features → fix the predictions.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-orchestrator-fix"
```

## PROBLEM 1: 7/35 features are NaN for all forex symbols

```bash
# Find which 7 features are NaN
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT * FROM vanguard_factor_matrix 
WHERE symbol = 'EUR/USD' 
AND cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
LIMIT 1
" 
```

Read the V3 factor engine to understand why:
```bash
grep -n "NaN\|null\|None\|warning.*nan\|high NaN" ~/SS/Vanguard/stages/vanguard_factor_engine.py | head -20
```

The 7 NaN features are likely SMC features (ob_proximity_5m, fvg_bullish_nearest, 
fvg_bearish_nearest, structure_break, liquidity_sweep, smc_premium_discount, 
htf_trend_direction) OR session features that fail outside market hours.

Check which features are NaN:
```bash
python3 -c "
import sqlite3
con = sqlite3.connect('/Users/sjani008/SS/Vanguard/data/vanguard_universe.db')
row = con.execute('''
    SELECT * FROM vanguard_factor_matrix 
    WHERE symbol = \"EUR/USD\" 
    AND cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
''').fetchone()
cols = [d[0] for d in con.execute('PRAGMA table_info(vanguard_factor_matrix)').fetchall()]
con.close()
for col, val in zip([c[1] for c in sorted(enumerate(cols), key=lambda x: x[0])], row):
    if val is None:
        print(f'  NaN: {col}')
    else:
        print(f'  OK:  {col} = {val}')
"
```

For each NaN feature, the fix depends on why it's NaN:
- **SMC features (smartmoneyconcepts library)**: May fail on forex symbols. 
  Fix: default to 0.0 when computation fails.
- **Session features**: May return None outside market hours.
  Fix: use last known value or default.
- **Volume features**: Forex has no real volume from Twelve Data.
  Fix: use tick_volume or default to 0.

In `vanguard_factor_engine.py`, find where each NaN feature is computed and add
explicit fallback to 0.0:

```python
# For EVERY feature computation, wrap in try/except with 0.0 default:
try:
    value = compute_feature(...)
except:
    value = 0.0

# Or after computing the full feature dict:
features = {k: (v if v is not None and not math.isnan(v) else 0.0) for k, v in features.items()}
```

## PROBLEM 2: Twelve Data API key loading

```bash
# Check current env
echo "TWELVE_DATA_API_KEY=$TWELVE_DATA_API_KEY"

# Check .env files
grep TWELVE_DATA ~/SS/Vanguard/.env 2>/dev/null
grep TWELVE_DATA ~/SS/.env.shared 2>/dev/null
grep TWELVE_DATA ~/.zshrc 2>/dev/null
```

The adapter reads from os.environ at import time. Fix:
```bash
# Find where the adapter loads the key
grep -n "TWELVE_DATA\|api_key\|apikey" ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py | head -10
```

Ensure the key is loaded from multiple sources with fallback:
```python
import os
from pathlib import Path
from dotenv import load_dotenv

# Load from multiple sources
load_dotenv(Path.home() / 'SS' / '.env.shared', override=False)
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env', override=True)

API_KEY = os.environ.get('TWELVE_DATA_API_KEY', '')
if not API_KEY:
    # Try reading from .env file directly
    env_path = Path.home() / 'SS' / 'Vanguard' / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith('TWELVE_DATA_API_KEY='):
                API_KEY = line.split('=', 1)[1].strip().strip("'\"")
                break
```

## PROBLEM 3: All predictions near-identical (model quality)

After fixing the NaN features, re-run and check prediction spread:
```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --force-regime ACTIVE 2>&1 | tail -20

# Then check spread
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, predicted_return, direction
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
ORDER BY predicted_return DESC
LIMIT 20
"
```

If predictions are STILL near-identical after NaN fix, the issue is model quality.
The forex regressors may have been trained on features that V3 doesn't compute 
the same way in production. Check feature alignment:

```python
import json, sqlite3
from pathlib import Path

# Model expects these features
meta = json.loads((Path.home() / 'SS/Vanguard/models/forex_regressors/meta.json').read_text())
model_features = meta['features']

# V3 produces these
con = sqlite3.connect(str(Path.home() / 'SS/Vanguard/data/vanguard_universe.db'))
cols = [r[1] for r in con.execute('PRAGMA table_info(vanguard_factor_matrix)').fetchall()]
con.close()

print(f'Model expects {len(model_features)} features:')
for f in model_features:
    status = 'OK' if f in cols else 'MISSING from V3'
    print(f'  {f}: {status}')
```

## PROBLEM 4: PREDICTION_MIN_ABS already fixed but not committed

The manual sed fix changed the threshold to 0. Commit it:
```bash
# Verify the fix is in place
grep "PREDICTION_MIN_ABS\|> 0\|< 0" ~/SS/Vanguard/stages/vanguard_selection_simple.py | head -5
```

If the lines show `> 0` and `< 0` instead of `> PREDICTION_MIN_ABS`, it's fixed.
If not, make the change:
```python
# In vanguard_selection_simple.py, replace the filtering lines:
# OLD: longs = grp[grp["predicted_return"] > PREDICTION_MIN_ABS]...
# NEW: longs = grp[grp["predicted_return"] > 0]...
# OLD: shorts = grp[grp["predicted_return"] < -PREDICTION_MIN_ABS]...  
# NEW: shorts = grp[grp["predicted_return"] < 0]...
```

## PROBLEM 5: Orchestrator loop mode

Check if --loop flag exists:
```bash
grep -n "loop\|interval\|sleep\|cycle\|continuous" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -15
```

The orchestrator should support:
```bash
# Single test cycle
python3 stages/vanguard_orchestrator.py --single-cycle --force-regime ACTIVE

# Continuous loop (production)
python3 stages/vanguard_orchestrator.py --loop --interval 300
```

If --loop doesn't exist, add it. The loop should:
1. Run one cycle (V1→V2→V3→V4B→V5→V6)
2. Sleep for `interval` seconds (default 300 = 5 min)
3. Check if within trading session hours before running
4. Repeat until killed

## Verification

After ALL fixes:
```bash
# Single cycle test
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --force-regime ACTIVE 2>&1 | tail -30

# Check NaN rate improved
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, 
    SUM(CASE WHEN session_vwap_distance IS NULL THEN 1 ELSE 0 END) as vwap_null,
    SUM(CASE WHEN ob_proximity_5m IS NULL THEN 1 ELSE 0 END) as ob_null,
    SUM(CASE WHEN fvg_bullish_nearest IS NULL THEN 1 ELSE 0 END) as fvg_null
FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
GROUP BY symbol LIMIT 5
"

# Check prediction variety
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT direction, COUNT(*) as n, 
    ROUND(AVG(predicted_return), 8) as avg_ret,
    ROUND(MIN(predicted_return), 8) as min_ret,
    ROUND(MAX(predicted_return), 8) as max_ret
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY direction
"

# Check shortlist has both LONG and SHORT
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT direction, COUNT(*) FROM vanguard_shortlist_v2
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist_v2)
GROUP BY direction
"
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: NaN features fallback, API key loading, prediction threshold, loop mode"
```
