# PHASE 2: V5 Model Routing, Fallback, and Scored Row Provenance
# V5 now routes to per-asset-class models with explicit fallback logic
# Give to Codex AFTER Phase 1 completes
# Date: Mar 31, 2026

---

## SCOPE

Phase 2 adds:
1. V5 model routing by asset_class + direction
2. Fallback logic (native → equity fallback → disabled)
3. Provenance metadata on every scored row in vanguard_shortlist
4. Readiness awareness in V5 scoring

Phase 2 does NOT change V6 risk gates — that's Phase 3.
Phase 1 (training) MUST be complete before this phase.

---

## READ FIRST

```bash
# What models exist after Phase 1
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT model_id, asset_class, direction, readiness, training_rows
FROM vanguard_model_registry ORDER BY asset_class
"

# Current V5 predict function
grep -n "predict\|model\|load\|lgbm" ~/SS/Vanguard/stages/vanguard_selection.py | head -20

# Current V5 shortlist schema
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_shortlist)"

# Feature profiles config
cat ~/SS/Vanguard/config/vanguard_feature_profiles.json
```

---

## 1. V5 MODEL ROUTING

### Routing key: asset_class + direction

```python
def get_model_for_scoring(asset_class: str, direction: str) -> dict:
    """
    Look up the best available model for this asset_class + direction.
    Returns: {model_id, model_family, model_source, readiness, feature_profile}
    """
    # 1. Check for native per-class model
    native_id = f"lgbm_{asset_class}_{direction}_v1"
    native = load_from_registry(native_id)

    if native and native["readiness"] in ("live_native", "validated_shadow"):
        return {
            "model_id": native_id,
            "model_family": f"lgbm_{asset_class}_{direction}",
            "model_source": "native",
            "readiness": native["readiness"],
            "feature_profile": native["feature_profile"],
        }

    # 2. Fallback to equity model
    fallback_id = f"lgbm_equity_{direction}_v1"
    fallback = load_from_registry(fallback_id)

    if fallback and fallback["readiness"] in ("live_native", "live_fallback"):
        return {
            "model_id": fallback_id,
            "model_family": f"lgbm_equity_{direction}",
            "model_source": "fallback_equity",
            "readiness": "live_fallback",
            "feature_profile": fallback["feature_profile"],
        }

    # 3. No model available
    return {
        "model_id": None,
        "model_family": None,
        "model_source": "none",
        "readiness": "not_trained",
        "feature_profile": None,
    }
```

### Scoring with routing

```python
def score_candidates(features_df, asset_class, direction):
    """Score candidates using the routed model."""
    routing = get_model_for_scoring(asset_class, direction)

    if routing["model_id"] is None:
        # No model available — return 0.0 probs, mark as unscored
        features_df[f"ml_prob"] = 0.0
        features_df["model_source"] = "none"
        features_df["model_readiness"] = "not_trained"
        return features_df

    # Load model
    model = load_model(routing["model_id"])

    # Apply feature profile (drop columns not in this model's training set)
    profile = load_feature_profile(routing["feature_profile"])
    drop_cols = profile.get("drop", [])
    feature_cols = [c for c in get_feature_columns(features_df) if c not in drop_cols]

    X = features_df[feature_cols].fillna(0)
    features_df["ml_prob"] = model.predict_proba(X)[:, 1]
    features_df["model_family"] = routing["model_family"]
    features_df["model_source"] = routing["model_source"]
    features_df["model_readiness"] = routing["readiness"]
    features_df["feature_profile"] = routing["feature_profile"]

    return features_df
```

---

## 2. UPDATED V5 FLOW

```
For each asset_class in feature matrix:
  1. Filter features to this asset_class
  2. Route to model: get_model_for_scoring(asset_class, "long")
  3. Score LONG candidates using routed model
  4. Route to model: get_model_for_scoring(asset_class, "short")
  5. Score SHORT candidates using routed model
  6. Apply ML gate (per-class threshold from config)
  7. Run asset-class strategies (existing V5 strategy router)
  8. Count consensus
  9. Write to vanguard_shortlist WITH provenance columns
```

---

## 3. SHORTLIST PROVENANCE COLUMNS

Add to `vanguard_shortlist` table:

```sql
ALTER TABLE vanguard_shortlist ADD COLUMN model_family TEXT;
ALTER TABLE vanguard_shortlist ADD COLUMN model_source TEXT;      -- "native" or "fallback_equity" or "none"
ALTER TABLE vanguard_shortlist ADD COLUMN model_readiness TEXT;   -- readiness state at time of scoring
ALTER TABLE vanguard_shortlist ADD COLUMN feature_profile TEXT;   -- which feature subset was used
ALTER TABLE vanguard_shortlist ADD COLUMN tbm_profile TEXT;       -- which TBM params trained the model
```

Every row in vanguard_shortlist now answers:
- **Which model scored it?** (model_family)
- **Was it native or fallback?** (model_source)
- **Is that model production-ready?** (model_readiness)
- **Which features were used?** (feature_profile)
- **What TBM trained it?** (tbm_profile)

---

## 4. ML GATE — PER-ASSET THRESHOLDS

Create `config/vanguard_ml_thresholds.json`:

```json
{
    "equity": {"long": 0.50, "short": 0.50},
    "forex": {"long": 0.50, "short": 0.50},
    "commodity": {"long": 0.50, "short": 0.50},
    "crypto": {"long": 0.50, "short": 0.50},
    "index": {"long": 0.50, "short": 0.50}
}
```

Start with 0.50 for all. Tune per-class after walk-forward validation shows different optimal thresholds.

---

## 5. FALLBACK RULES (FROM GPT QA REVIEW)

| Scenario | Action |
|---|---|
| Native model exists + readiness=live_native | Use native model |
| Native model exists + readiness=validated_shadow | Use native for shadow scoring, write to shortlist but mark readiness |
| Native model exists + readiness=trained_insufficient_validation | Do NOT use. Fall back to equity. |
| No native model exists | Fall back to equity model |
| No equity model exists | Score as 0.0, mark model_source="none" |
| Fallback in effect | MUST be visible in model_source field |

---

## 6. VANGUARD ADAPTER UPDATE

File: `~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py`

Include the new provenance fields in the adapter output:

```python
candidates.append({
    # ... existing fields ...
    "native": {
        # ... existing native fields ...
        "model_family": row["model_family"],
        "model_source": row["model_source"],
        "model_readiness": row["model_readiness"],
        "feature_profile": row["feature_profile"],
        "tbm_profile": row["tbm_profile"],
    },
})
```

---

## VERIFY

```bash
# After V5 update, run single cycle
cd ~/SS/Vanguard && python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE

# Check provenance in shortlist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, direction, model_family, model_source, model_readiness
FROM vanguard_shortlist LIMIT 10
"

# Check that fallback is explicit
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT model_source, COUNT(*) FROM vanguard_shortlist GROUP BY model_source
"
```
