# PHASE 3: V6 Readiness-Aware Risk Gates + QA Promotion + UI Provenance
# V6 gate behavior changes based on model readiness. QA promotion standards.
# Give to Codex AFTER Phase 2 completes
# Date: Mar 31, 2026

---

## SCOPE

Phase 3 adds:
1. V6 risk gate behavior that respects model readiness (native vs fallback vs shadow)
2. QA promotion rules — minimum standards before a per-class model goes live
3. Gate decision metadata on every tradeable_portfolio row
4. UI provenance requirements for Vanguard scored rows

Phase 1 (training) and Phase 2 (routing) MUST be complete first.

---

## 1. V6 READINESS-AWARE GATE BEHAVIOR

V6 pre-execution gate (in trade_desk.py) and staged V6 (vanguard_risk_filters.py)
must both understand model readiness:

### Gate rules by readiness state:

| model_readiness | Gate behavior | Execution allowed? |
|---|---|---|
| `live_native` | Normal threshold policy per asset class | Yes |
| `live_fallback` | Stricter threshold (+0.05 above normal) | Yes, with fallback tag |
| `validated_shadow` | Score tracked but NOT executed | No — forward-track only |
| `trained_insufficient_validation` | Blocked | No |
| `not_trained` | Blocked | No |
| `disabled` | Blocked | No |

### Implementation in V6:

```python
def apply_readiness_gate(candidate, account_profile):
    readiness = candidate.get("model_readiness", "not_trained")
    base_threshold = get_ml_threshold(candidate["asset_class"], candidate["direction"])

    if readiness == "live_native":
        threshold = base_threshold  # Normal
        allow_execution = True
    elif readiness == "live_fallback":
        threshold = base_threshold + 0.05  # Stricter for fallback
        allow_execution = True
    elif readiness == "validated_shadow":
        threshold = base_threshold
        allow_execution = False  # Forward-track only
    else:
        threshold = 1.0  # Effectively blocked
        allow_execution = False

    passed = candidate["ml_prob"] >= threshold

    return {
        "passed": passed and allow_execution,
        "reason": f"readiness={readiness}, threshold={threshold}, prob={candidate['ml_prob']:.3f}",
        "execution_allowed": allow_execution,
        "gate_policy": f"readiness_gate_{readiness}",
        "fallback_in_effect": readiness == "live_fallback",
    }
```

---

## 2. GATE DECISION METADATA

Add to `vanguard_tradeable_portfolio`:

```sql
ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN model_readiness TEXT;
ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN model_source TEXT;
ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN gate_policy TEXT;
ALTER TABLE vanguard_tradeable_portfolio ADD COLUMN fallback_in_effect INTEGER DEFAULT 0;
```

Every row now explains:
- **model_readiness** — was this scored by a live_native or live_fallback model?
- **model_source** — native or fallback_equity?
- **gate_policy** — which threshold policy was applied?
- **fallback_in_effect** — was a fallback model used? (0/1)

---

## 3. QA PROMOTION RULES

No per-class model goes live just because it can be loaded.
Each asset class must meet ALL of these before promotion to `live_native`:

| Metric | Minimum Standard |
|---|---|
| Training rows | ≥ 5,000 |
| Walk-forward windows | ≥ 5 |
| Mean IC (long) | > 0.02 |
| Mean IC (short) | > 0.02 |
| WR at P>0.55 (long) | > 33.3% (breakeven at 2:1 R/R) |
| WR at P>0.55 (short) | > 33.3% |
| IC stability (std across windows) | < 0.03 (not wildly inconsistent) |
| Explicit sign-off | Operator must promote manually |

### Promotion flow:

```
not_trained → trained_insufficient_validation → validated_shadow → live_native
                                                                     ↑
                                                              requires manual
                                                              operator sign-off
```

### Promotion CLI:

```bash
# Check which models are ready for promotion
python3 stages/vanguard_model_trainer.py --promotion-check

# Promote a model
python3 stages/vanguard_model_trainer.py --promote lgbm_forex_long_v1

# Demote back to shadow
python3 stages/vanguard_model_trainer.py --demote lgbm_forex_long_v1
```

---

## 4. STAGED ROLLOUT PLAN

### Stage A (now): Conservative metadata
- Phase 1 trains per-class models, writes to registry with readiness
- All new models start as `trained_insufficient_validation`
- Equity models remain `live_native` (existing, proven)
- All non-equity rows use `live_fallback` (equity model)

### Stage B (when data sufficient): Shadow validation
- Models with ≥5,000 rows + ≥5 WF windows promoted to `validated_shadow`
- Shadow models score rows but V6 blocks execution
- Forward-tracked alongside live_fallback results
- Operator compares shadow vs fallback quality

### Stage C (when validated): Selective promotion
- Models meeting ALL QA standards promoted to `live_native`
- Operator sign-off required
- Likely order: forex first (most data), then crypto, then commodity, index last

### Stage D (future): Richer UI
- Model comparison surfaces per asset class
- Cross-class quality dashboards
- Only after ≥3 months of live_native evidence

---

## 5. UI PROVENANCE REQUIREMENTS

### Candidate table (Vanguard rows):

Every Vanguard candidate row in the UI should expose (in detail panel or tooltip):

| Field | What it shows | Example |
|---|---|---|
| asset_class | Which asset group | forex |
| model_family | Which model scored it | lgbm_forex_long |
| model_source | Native or fallback | fallback_equity |
| model_readiness | Production state | live_fallback |
| feature_profile | Feature subset used | fx_features_v1 |
| tbm_profile | TBM params used | tbm_forex_v1 |

### Combined mode:

- S1 rows show S1 provenance (scorer_predictions + convergence)
- Meridian rows show Meridian provenance (shortlist_daily + predictions_daily)
- Vanguard rows show model provenance (model_family + model_source + readiness)
- These are NOT comparable across lanes

### Null handling:

- Missing model_source = NULL, not "native"
- Missing readiness = NULL, not "live"
- Do not fake provenance to look complete

---

## 6. QA ACCEPTANCE MATRIX (FROM GPT REVIEW)

| Area | Must be true | Fail condition | Priority |
|---|---|---|---|
| Model provenance | Every Vanguard row exposes asset_class, model_family, model_source, readiness | Row can't explain which model scored it | P0 |
| Fallback truth | Fallback rows explicitly marked | Fallback indistinguishable from native | P0 |
| Promotion rules | Native models can't go live without validation | Model promoted with thin data | P0 |
| UI semantics | Combined blocks unsafe cross-score comparisons | User sorts all lanes by non-comparable scores | P0 |
| Null handling | Missing values stay missing | UI silently invents values | P0 |
| Readiness visibility | Per-class readiness visible in backend/monitoring | All classes look equally ready | P1 |
| Auditability | TBM profile and feature profile recorded | Can't reproduce training context | P1 |

---

## VERIFY

```bash
# Model readiness states
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, readiness, COUNT(*) as models
FROM vanguard_model_registry GROUP BY asset_class, direction, readiness
"

# Gate decisions in tradeable portfolio
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, model_readiness, model_source, gate_policy, COUNT(*)
FROM vanguard_tradeable_portfolio GROUP BY account_id, model_readiness, model_source, gate_policy
"

# Promotion check
python3 stages/vanguard_model_trainer.py --promotion-check

# End-to-end: single cycle with provenance
cd ~/SS/Vanguard && python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE
sqlite3 data/vanguard_universe.db "
SELECT symbol, asset_class, model_family, model_source, model_readiness
FROM vanguard_shortlist ORDER BY asset_class LIMIT 20
"
```
