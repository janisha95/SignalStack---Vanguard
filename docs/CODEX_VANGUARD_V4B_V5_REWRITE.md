# CODEX — Vanguard V4B/V5 Rewrite + Meridian Prefilter Bug Fix

## TWO JOBS IN THIS PROMPT:
1. Fix Meridian prefilter — bond ETFs still passing after 2 full orchestrator runs
2. Vanguard V4B scorer + V5 simple selection rewrite

---

# JOB 1: FIX MERIDIAN PREFILTER (P0 — blocking daily pipeline)

## The Problem
Bond/money-market ETFs (FLOT, VUSB, GSY, FLRN, USFR, ICSH, JPST, FTSM, PULS)
are STILL in the Meridian shortlist after 2 full orchestrator runs.

Commit `2e9c491` added a blocklist to `v2_prefilter.py`, but it's not working.

## Debug Steps

```bash
# 1. Is the blocklist in the code?
grep -n "BOND_MONEY_MARKET\|FLOT\|VUSB\|blocklist" ~/SS/Meridian/stages/v2_prefilter.py | head -10

# 2. Are these tickers in the latest prefilter_results?
sqlite3 ~/SS/Meridian/data/v2_universe.db "
SELECT date, ticker FROM prefilter_results
WHERE ticker IN ('FLOT','VUSB','GSY','FLRN','USFR','ICSH','JPST','FTSM','PULS')
AND date = (SELECT MAX(date) FROM prefilter_results)
"

# 3. When was prefilter_results last updated?
sqlite3 ~/SS/Meridian/data/v2_universe.db "
SELECT MAX(date), COUNT(*) FROM prefilter_results
"

# 4. Does Stage 3 read from prefilter_results or recompute?
grep -n "prefilter_results\|prefilter_cache\|load_prefilter\|_load_latest_prefilter" ~/SS/Meridian/stages/v2_orchestrator.py | head -10

# 5. Does Stage 3 skip if prefilter is cached?
grep -n "skip\|cache\|stale\|reuse\|existing" ~/SS/Meridian/stages/v2_factor_engine.py | head -10
```

## Root Cause Hypothesis
The orchestrator likely caches prefilter_results from a previous run and Stage 3
reads the cached version instead of re-running the prefilter. Or the blocklist
is checked AFTER the prefilter writes to DB, not before.

## Fix
Whatever the root cause, the fix must ensure:
1. The blocklist is applied BEFORE writing to prefilter_results
2. If prefilter_results has cached data, re-run the prefilter when orchestrator runs
3. After fix, verify FLOT/VUSB/GSY are NOT in prefilter_results

```bash
# After fixing, force a full prefilter re-run:
cd ~/SS/Meridian
sqlite3 data/v2_universe.db "DELETE FROM prefilter_results WHERE date = (SELECT MAX(date) FROM prefilter_results)"
python3 stages/v2_prefilter.py
# Check
sqlite3 data/v2_universe.db "
SELECT COUNT(*) as total,
    SUM(CASE WHEN ticker IN ('FLOT','VUSB','GSY','FLRN','USFR','ICSH','JPST','FTSM','PULS') THEN 1 ELSE 0 END) as bond_etfs
FROM prefilter_results WHERE date = (SELECT MAX(date) FROM prefilter_results)
"
# bond_etfs should be 0

# Then re-run full pipeline:
python3 stages/v2_orchestrator.py --real-ml 2>&1 | tail -30

# Verify shortlist:
sqlite3 data/v2_universe.db "
SELECT ticker, direction, final_score FROM shortlist_daily
WHERE date = (SELECT MAX(date) FROM shortlist_daily) AND direction = 'LONG'
ORDER BY final_score DESC LIMIT 10
"
# Should NOT contain FLOT, VUSB, GSY, etc.
```

## Also add inverse/leveraged ETFs to blocklist
While fixing, add these to the blocklist as well:
```python
# Inverse/leveraged ETFs
'MSTZ', 'SQQQ', 'TQQQ', 'SPXS', 'SPXL', 'SARK', 'ARKK',
'QLD', 'QID', 'SSO', 'SDS', 'UVXY', 'SVXY', 'VIXY', 'VXX',
# Sector index ETFs (not individual stocks)
'QTEC', 'XLY', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLB', 'XLP', 'XLU',
'IWM', 'SPY', 'QQQ', 'DIA', 'VOO', 'VTI', 'IVV',
```

## Commit
```bash
cd ~/SS/Meridian && git add -A && git commit -m "fix: prefilter bond ETF blocklist not applied — force re-run"
```

---

# JOB 2: VANGUARD V4B SCORER + V5 SIMPLE SELECTION

## Context
Models are ALREADY TRAINED and sitting in `~/SS/Vanguard/models/`.
We need new V4B (inference scorer) and new V5 (simple ranking).

## Step 0: Verify model files exist in the repo

```bash
echo "=== Existing model files ==="
find ~/SS/Vanguard/models/ -name "*.pkl" -o -name "*.json" -o -name "*.pt" | sort

echo ""
echo "=== Equity regressors ==="
ls -la ~/SS/Vanguard/models/equity_regressors/ 2>/dev/null || echo "Dir not found"
ls -la ~/SS/Vanguard/models/*equity*regressor* 2>/dev/null || echo "No loose files"

echo ""
echo "=== Forex regressors ==="
ls -la ~/SS/Vanguard/models/forex_regressors/ 2>/dev/null || echo "Dir not found"
ls -la ~/SS/Vanguard/models/*forex*regressor* 2>/dev/null || echo "No loose files"

echo ""
echo "=== Meta files ==="
find ~/SS/Vanguard/models/ -name "*meta*" -o -name "*config*" | sort
```

Organize models into directories if they're not already:
```bash
mkdir -p ~/SS/Vanguard/models/equity_regressors
mkdir -p ~/SS/Vanguard/models/forex_regressors

# Move equity regressors (check if they exist first, may be in different locations)
for f in lgbm_equity_regressor_v1.pkl ridge_equity_regressor_v1.pkl et_equity_regressor_v1.pkl lgbm_sw_equity_regressor_v1.pkl equity_regressor_meta.json; do
    found=$(find ~/SS/Vanguard/models/ -name "$f" -not -path "*/equity_regressors/*" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        cp "$found" ~/SS/Vanguard/models/equity_regressors/
        echo "Copied $f to equity_regressors/"
    fi
done

# Move forex regressors
for f in rf_forex_regressor_v1.pkl lgbm_forex_regressor_v1.pkl vanguard_forex_model_meta.json; do
    found=$(find ~/SS/Vanguard/models/ -name "$f" -not -path "*/forex_regressors/*" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        cp "$found" ~/SS/Vanguard/models/forex_regressors/
        echo "Copied $f to forex_regressors/"
    fi
done

# Rename meta files to meta.json
[ -f ~/SS/Vanguard/models/equity_regressors/equity_regressor_meta.json ] && \
    cp ~/SS/Vanguard/models/equity_regressors/equity_regressor_meta.json ~/SS/Vanguard/models/equity_regressors/meta.json
[ -f ~/SS/Vanguard/models/forex_regressors/vanguard_forex_model_meta.json ] && \
    cp ~/SS/Vanguard/models/forex_regressors/vanguard_forex_model_meta.json ~/SS/Vanguard/models/forex_regressors/meta.json

echo ""
echo "=== Final layout ==="
ls -la ~/SS/Vanguard/models/equity_regressors/
ls -la ~/SS/Vanguard/models/forex_regressors/
```

## Step 1: Create vanguard_scorer.py (V4B — inference only)

Create: `~/SS/Vanguard/stages/vanguard_scorer.py`

This loads pre-trained regressor .pkl files, scores V3 survivors, writes predictions.

**Equity ensemble:** 0.4×LGBM(IC 0.034) + 0.3×Ridge(IC 0.025) + 0.3×ET(IC 0.025)
**Equity features (29):** session_vwap_distance, premium_discount_zone, gap_pct,
session_opening_range_position, daily_drawdown_from_high, momentum_3bar, momentum_12bar,
momentum_acceleration, atr_expansion, daily_adx, relative_volume, volume_burst_z,
down_volume_ratio, effort_vs_result, rs_vs_benchmark_intraday, daily_rs_vs_benchmark,
benchmark_momentum_12bar, cross_asset_correlation, session_phase, ob_proximity_5m,
fvg_bullish_nearest, fvg_bearish_nearest, structure_break, liquidity_sweep,
smc_premium_discount, htf_trend_direction, htf_structure_break, htf_fvg_nearest,
htf_ob_proximity

**Forex ensemble:** avg(RF(IC 0.049), LGBM(IC 0.037))
**Forex features (27):** session_vwap_distance, premium_discount_zone, gap_pct,
session_opening_range_position, daily_drawdown_from_high, momentum_3bar, momentum_12bar,
momentum_acceleration, atr_expansion, daily_adx, volume_burst_z, down_volume_ratio,
rs_vs_benchmark_intraday, daily_rs_vs_benchmark, benchmark_momentum_12bar,
cross_asset_correlation, session_phase, ob_proximity_5m, fvg_bullish_nearest,
fvg_bearish_nearest, structure_break, liquidity_sweep, smc_premium_discount,
htf_trend_direction, htf_structure_break, htf_fvg_nearest, htf_ob_proximity

**Logic:**
1. Read latest `vanguard_factor_matrix` from DB
2. For each symbol, look up asset_class from `universe_members`
3. Load the right models for that asset_class
4. Extract feature columns, fill missing with 0.0
5. Ensemble predict → predicted_return (positive = LONG, negative = SHORT)
6. Write to `vanguard_predictions` table

**Output table:**
```sql
CREATE TABLE IF NOT EXISTS vanguard_predictions (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    predicted_return REAL,
    direction TEXT,
    model_id TEXT,
    scored_at TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol)
);
```

**CLI:**
```
python3 stages/vanguard_scorer.py              # score latest cycle
python3 stages/vanguard_scorer.py --dry-run    # print without DB write
python3 stages/vanguard_scorer.py --asset-class forex
```

## Step 2: Create vanguard_selection_simple.py (V5 — simple ranking)

Create: `~/SS/Vanguard/stages/vanguard_selection_simple.py`

**Logic:**
1. Read `vanguard_predictions` for latest cycle
2. Per asset_class:
   - LONG: predicted_return > 0.0001, sorted DESC, store top 30
   - SHORT: predicted_return < -0.0001, sorted ASC (most negative first), store top 30
3. Display top 5 per side
4. Write to `vanguard_shortlist_v2` table

**Output table:**
```sql
CREATE TABLE IF NOT EXISTS vanguard_shortlist_v2 (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT,
    direction TEXT NOT NULL,
    predicted_return REAL,
    rank INTEGER,
    model_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, symbol, direction)
);
```

**CLI:**
```
python3 stages/vanguard_selection_simple.py              # full run
python3 stages/vanguard_selection_simple.py --dry-run
python3 stages/vanguard_selection_simple.py --top-n 30 --show-n 5
```

## Step 3: Wire into V7 orchestrator

```bash
grep -n "model_trainer\|vanguard_selection\|V4B\|V5\|import.*selection\|import.*trainer\|import.*scorer" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

Replace old V4B/V5 calls:
- Old V4B (`vanguard_model_trainer.py`) → New `vanguard_scorer.py`
- Old V5 (`vanguard_selection.py`) → New `vanguard_selection_simple.py`

Keep V1→V2→V3 and V6→V7 unchanged.

## Verification

```bash
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_scorer.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_selection_simple.py

# Check models load
python3 -c "
import joblib
from pathlib import Path
mdir = Path.home() / 'SS/Vanguard/models'
for d in ['equity_regressors', 'forex_regressors']:
    p = mdir / d
    if p.exists():
        pkls = list(p.glob('*.pkl'))
        print(f'{d}: {len(pkls)} models')
        for f in pkls:
            m = joblib.load(f)
            print(f'  {f.name}: {type(m).__name__}')
    else:
        print(f'{d}: NOT FOUND')
"

# Check factor matrix has data
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT cycle_ts_utc, COUNT(*) FROM vanguard_factor_matrix
GROUP BY cycle_ts_utc ORDER BY cycle_ts_utc DESC LIMIT 3
"

# Dry run
cd ~/SS/Vanguard && python3 stages/vanguard_scorer.py --dry-run
cd ~/SS/Vanguard && python3 stages/vanguard_selection_simple.py --dry-run

# Full run
cd ~/SS/Vanguard && python3 stages/vanguard_scorer.py
cd ~/SS/Vanguard && python3 stages/vanguard_selection_simple.py
```

## Commit
```bash
cd ~/SS/Meridian && git add -A && git commit -m "fix: prefilter bond ETF blocklist enforcement"
cd ~/SS/Vanguard && git add -A && git commit -m "feat: V4B regressor scorer + V5 simple selection"
```
