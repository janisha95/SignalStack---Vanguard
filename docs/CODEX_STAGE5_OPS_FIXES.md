# CODEX — Stage 5 Operational Fixes
# Stage 5 is code-complete but has 5 operational gaps
# Based on GPT Stage 5 Go-Live Gap Analysis
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Model artifacts
ls ~/SS/Vanguard/models/vanguard/latest/*.pkl 2>/dev/null
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT model_id, readiness, training_rows FROM vanguard_model_registry"

# 2. Feature freshness
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT MAX(cycle_ts_utc), COUNT(*) FROM vanguard_features"

# 3. Universe members
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT asset_class, COUNT(*) FROM vanguard_universe_members WHERE is_active=1 GROUP BY asset_class"

# 4. Current V5 model loading
grep -n "load_model\|_load_model\|predict\|0\.0\|None" ~/SS/Vanguard/stages/vanguard_selection.py | head -20

# 5. Current shortlist schema
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_shortlist)"
```

---

## FIX 1: Explicit model-missing failure mode (Gap 1 + Gap 5)

When model artifacts are missing, V5 currently defaults probabilities to 0.0
and the ML gate silently eliminates everything. The operator sees "0 candidates"
with no explanation of WHY.

Fix: when models are missing, write explicit status to the cycle output:

```python
# In vanguard_selection.py, after model loading attempt:
def run_selection(features_df, cycle_ts, force_regime=None, dry_run=False):
    # ... existing code ...
    
    # Check model availability BEFORE scoring
    model_status = check_model_availability()
    
    if not model_status["any_model_available"]:
        logger.warning(f"[V5] NO MODELS AVAILABLE — shortlist will be empty")
        logger.warning(f"[V5] Missing: {model_status['missing']}")
        logger.warning(f"[V5] Run V4B model training first: python3 stages/vanguard_model_trainer.py --track A")
        
        # Write a diagnostic row to shortlist so downstream knows WHY it's empty
        write_diagnostic_row(cycle_ts, "NO_MODELS", "No trained model artifacts found")
        return pd.DataFrame()  # Empty but explained
    
    # Log which models are being used
    for ac, info in model_status["models"].items():
        logger.info(f"[V5] {ac}: {info['model_id']} ({info['source']}, {info['readiness']})")

def check_model_availability():
    """Check which models exist and are usable."""
    from vanguard.helpers.db import VanguardDB
    con = VanguardDB.connect()
    
    models = {}
    missing = []
    any_available = False
    
    for asset_class in ["equity", "forex", "commodity", "crypto", "index"]:
        for direction in ["long", "short"]:
            # Check native model
            native_id = f"lgbm_{asset_class}_{direction}_v1"
            row = con.execute(
                "SELECT readiness FROM vanguard_model_registry WHERE model_id=?",
                (native_id,)
            ).fetchone()
            
            if row and row[0] in ("live_native", "validated_shadow", "trained_insufficient_validation"):
                models[f"{asset_class}_{direction}"] = {
                    "model_id": native_id, "source": "native", "readiness": row[0]
                }
                any_available = True
            else:
                # Check equity fallback
                fallback_id = f"lgbm_equity_{direction}_v1"
                fb_row = con.execute(
                    "SELECT readiness FROM vanguard_model_registry WHERE model_id=?",
                    (fallback_id,)
                ).fetchone()
                if fb_row:
                    models[f"{asset_class}_{direction}"] = {
                        "model_id": fallback_id, "source": "fallback_equity", "readiness": fb_row[0]
                    }
                    any_available = True
                else:
                    missing.append(f"{asset_class}_{direction}")
    
    con.close()
    return {"any_model_available": any_available, "models": models, "missing": missing}
```

---

## FIX 2: Feature freshness guard (Gap 2)

V5 should refuse to score on stale features:

```python
# Before scoring, check feature freshness
def check_feature_freshness(max_age_minutes=30):
    """Verify features are fresh enough for live scoring."""
    con = sqlite3.connect(DB_PATH)
    latest = con.execute("SELECT MAX(cycle_ts_utc) FROM vanguard_features").fetchone()[0]
    con.close()
    
    if not latest:
        return False, "No features in DB"
    
    from datetime import datetime, timezone
    latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 60
    
    if age > max_age_minutes:
        return False, f"Features are {age:.0f} min old (max {max_age_minutes})"
    
    return True, f"Features are {age:.1f} min old"

# In run_selection:
fresh, reason = check_feature_freshness()
if not fresh:
    logger.warning(f"[V5] STALE FEATURES: {reason}")
    # Still run but mark output as stale
    # Don't hard-block — operator may want stale results for debugging
```

---

## FIX 3: Asset class fallback logging (Gap 3)

When a symbol's asset_class is missing from universe_members, log it:

```python
# In the routing step, when looking up asset_class:
def get_asset_class(symbol):
    """Get asset class from universe_members. Log if missing."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT asset_class FROM vanguard_universe_members WHERE symbol=? AND is_active=1",
        (symbol,)
    ).fetchone()
    con.close()
    
    if row:
        return row[0]
    
    # Fallback to equity with warning
    logger.warning(f"[V5] {symbol}: no asset_class in universe_members, defaulting to equity")
    return "equity"
```

---

## FIX 4: V5→V6 contract validation (Gap 4)

After writing shortlist, verify V6 can read it:

```python
# At the end of run_selection, after writing shortlist:
def validate_v5_v6_contract(cycle_ts):
    """Verify V6 can read what V5 wrote."""
    con = sqlite3.connect(DB_PATH)
    
    # Check required columns exist
    columns = {row[1] for row in con.execute("PRAGMA table_info(vanguard_shortlist)").fetchall()}
    required = {"cycle_ts_utc", "symbol", "direction", "strategy", "strategy_score", "ml_prob", "consensus_count"}
    missing = required - columns
    
    if missing:
        logger.error(f"[V5] Contract violation: shortlist missing columns {missing}")
        return False
    
    # Check rows were written
    count = con.execute(
        "SELECT COUNT(*) FROM vanguard_shortlist WHERE cycle_ts_utc=?", (cycle_ts,)
    ).fetchone()[0]
    
    con.close()
    
    if count == 0:
        logger.warning(f"[V5] 0 shortlist rows for cycle {cycle_ts}")
        return True  # Valid but empty
    
    logger.info(f"[V5] Contract OK: {count} shortlist rows for V6")
    return True
```

---

## FIX 5: CAUTION regime raises threshold (non-blocking gap)

If regime is CAUTION, increase the ML gate threshold by 0.05:

```python
# In ML gate application:
def apply_ml_gate(candidates, asset_class, direction, regime):
    base_threshold = load_threshold(asset_class, direction)
    
    if regime == "CAUTION":
        threshold = base_threshold + 0.05
        logger.info(f"[V5] CAUTION regime: threshold raised {base_threshold} → {threshold}")
    else:
        threshold = base_threshold
    
    return candidates[candidates["ml_prob"] >= threshold]
```

---

## VERIFY

```bash
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_selection.py

# Single cycle with model status logging
cd ~/SS/Vanguard && python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE 2>&1 | grep "\[V5\]"

# Check shortlist has provenance
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, model_family, model_source, model_readiness
FROM vanguard_shortlist ORDER BY cycle_ts_utc DESC LIMIT 5
"
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: V5 operational gaps — model status, feature freshness, asset fallback logging, V6 contract check"
```
