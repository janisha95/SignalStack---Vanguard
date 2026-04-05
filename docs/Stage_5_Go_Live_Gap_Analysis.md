# Stage 5 Go-Live Gap Analysis
**SignalStack / Vanguard final build**  
**Purpose:** assess whether Stage 5 is go-live ready as the production selection / shortlist layer, identify the real blockers, and define the honest operator/runtime stance.

---

## 1. Executive Summary

Stage 5 is **structurally complete**, but not automatically go-live ready.

### What is real
- main selection module exists
- regime detector exists
- ML probability scoring exists
- strategy router exists
- consensus counter exists
- output table exists
- all strategy files are present and registered

### Real go-live issue
The biggest blocker is **not missing strategy code**.  
The biggest blocker is **data and ops truth**:

- if trained model artifacts are missing, all probabilities default to `0.0`
- if all probabilities are `0.0`, the ML gate eliminates every candidate
- if `vanguard_features` is stale or empty, Stage 5 produces no useful shortlist
- if `vanguard_universe_members` is empty or incomplete, non-equity routing collapses toward equity defaults

### Current go-live stance
- **Code structure:** strong
- **Operational readiness:** conditional
- **Recommended stance:** **staged go-live only after model and universe prerequisites are proven**

---

## 2. Current Stage 5 Truth

### What Stage 5 is supposed to do
- apply regime gate
- score candidates with ML probability layer
- route symbols to appropriate strategies by asset context
- count consensus
- write shortlist rows into `vanguard_shortlist`

### What Stage 5 actually does
When upstream inputs are present, it does that.

When upstream inputs are missing or weak:
- probabilities default to zero if models are absent
- shortlist may be empty
- routing may degrade if asset class membership is missing
- no hard error is necessarily raised

### Current source inputs
- `vanguard_features` from V3
- `vanguard_universe_members` for asset class context
- `models/vanguard/latest/*.pkl` model artifacts
- `vanguard_model_registry`
- static strategy config
- static selection config

### Current output
- `vanguard_shortlist`
- primary key: `(cycle_ts_utc, symbol, strategy)`
- return status values such as:
  - `ok`
  - `no_features`
  - `skipped`
  - `no_results`

---

## 3. Strengths

## 3.1 Strategy library is implemented
All expected strategy classes exist and are registered.

## 3.2 Router/regime/consensus stack is real
The key moving parts exist and are wired:
- regime detector
- strategy router
- consensus counter
- probability scoring layer

## 3.3 Output schema is real and stable enough
The shortlist table exists and code writes to it.  
Schema migration support is also present.

## 3.4 Extra metadata columns are directionally good
Fields like:
- `model_family`
- `model_source`
- `model_readiness`
- `feature_profile`
- `tbm_profile`

are useful additions, even if they go beyond the older spec.

## 3.5 Debug/test ergonomics exist
The dry-run and debug-symbol flows are useful for operator/QA work.

---

## 4. Main Gaps

## Gap 1 — Model artifacts may not exist
This is the biggest real blocker.

If model files are missing from `models/vanguard/latest/*.pkl`:
- `_load_model()` returns `None`
- probabilities become `0.0`
- ML gate removes everything
- shortlist writes zero rows
- downstream sees “no candidates” instead of explicit model failure

### Why it matters
This is a **production-truth risk**.  
The stage can look “healthy” in code while being operationally dead.

---

## Gap 2 — Feature freshness is not enforced
Stage 5 reads the latest available `vanguard_features`, but there is no strong freshness guard.

### Why it matters
A selection layer should not silently operate on stale features and present that as current shortlist truth.

---

## Gap 3 — Asset class depends on `vanguard_universe_members`
If `vanguard_universe_members` is empty or weak:
- non-equity symbols can fall back to `equity`
- strategy routing becomes wrong
- per-asset model logic becomes misleading

### Why it matters
This is a hidden routing truth problem.

---

## Gap 4 — V5 ↔ V6 contract is not independently proven here
Stage 5 writes shortlist rows for Stage 6 to consume.

But the exact downstream read contract has not been fully validated here.

### Why it matters
A shortlist stage cannot be called production-ready if its downstream consumer contract is assumed rather than proven.

---

## Gap 5 — Silent no-result behavior
When models are absent or gating eliminates everything, Stage 5 can produce zero rows without surfacing a strong failure mode.

### Why it matters
Silent emptiness is dangerous in production systems because operators may misread “no results” as valid market outcome instead of setup failure.

---

## 5. Selection / Ranking Truth Gaps

## 5.1 Ranking appears native, which is good
Ranking is generated inside strategy scoring logic, not as an obviously fake post-hoc universal rank.

That is a strength.

## 5.2 `strategies_matched` shape is weak
If it is stored as a comma-joined string instead of a structured list/array, downstream parsing becomes fragile.

## 5.3 `primary_strategy` / `primary_rank` are missing
These are in the intended output idea but not clearly represented in the actual stored shortlist contract.

### Why it matters
Downstream consumers and UI logic may expect stronger “winner strategy” semantics than the current contract really provides.

## 5.4 CAUTION regime behavior is incomplete
If CAUTION only changes the regime label but not actual threshold behavior, then the regime gate is less meaningful than the spec implies.

---

## 6. Contract Gaps

### Gap A — shortlist contract is richer than spec in some places
This is acceptable if documented.

### Gap B — shortlist contract is poorer than spec in some places
Missing or weak areas include:
- `primary_strategy`
- `primary_rank`
- stronger structured matched-strategy payloads
- explicit failure/reason semantics when no models exist

### Gap C — model-source truth must be operator-visible
If model provenance is written but not surfaced, it still leaves operators blind.

---

## 7. Go-Live Blockers

These block calling Stage 5 production-ready.

### Blocker 1
Model artifacts must exist in the expected live path.

### Blocker 2
`vanguard_universe_members` must have correct asset class coverage for non-equity routing.

### Blocker 3
Stage 5 must prove it can produce non-zero rows under real conditions with real model source values.

### Blocker 4
Stage 5 → Stage 6 shortlist contract must be validated directly.

---

## 8. Non-Blocking Gaps

These matter, but they are not the first blockers:

- missing `primary_strategy` and `primary_rank`
- CAUTION regime not altering thresholds strongly enough
- `strategies_matched` string shape
- no performance instrumentation for the `<10s` target
- LLM overlay untested (while OFF by default)

---

## 9. UI / Operator Truth Gaps

Operators should be able to know:
- whether model artifacts were loaded
- whether native or fallback model was used
- which asset class the row was routed under
- whether shortlist was empty because market conditions were poor or because models were absent
- whether feature inputs were fresh

Operators should **not** be led to believe:
- a zero-row shortlist automatically means “no valid opportunities”
- model/routing truth is healthy if all probabilities defaulted to zero
- non-equity routing is trustworthy if asset class membership is missing

---

## 10. QA Go-Live Checks

Before go-live, verify:

### Model truth
- required `.pkl` files exist
- at least equity long/short native models exist
- non-equity native or fallback paths behave as expected

### Universe/routing truth
- `vanguard_universe_members` contains correct asset classes
- non-equity symbols do not default silently to equity without warning
- forex symbols route to forex strategies
- other asset classes route correctly or fail explicitly

### Output truth
- Stage 5 produces non-zero rows under controlled dry-run conditions
- `model_source` is not `none` on valid rows
- shortlist row counts and strategies are sensible
- downstream Stage 6 can read the shortlist without contract mismatch

### Freshness truth
- features are from a recent/current cycle
- stale features are detectable or warned on

---

## 11. Go-Live Recommendation

### Judgment
**Staged go-live is feasible.**

### Conditions
Only if the following are true:
1. model artifacts exist
2. universe membership is populated correctly
3. Stage 5 produces real non-zero rows
4. Stage 5 → Stage 6 contract is verified
5. operators can tell when shortlist emptiness is a setup issue vs a real market outcome

### Final stance
Stage 5 is **closer to go-live than V2/V3**, but it is not automatically green just because the strategy code exists.

The correct description is:

> Stage 5 is code-complete enough for staged go-live, but operational readiness depends on model artifact availability, valid universe membership, and verified shortlist-to-V6 contract integrity.

---

## 12. Final Judgment

### Code state
Strong

### Runtime state
Conditional

### Go-live state
**Go with conditions / staged go-live only**

Do not call it fully production-ready until model, routing, and downstream contract truth are proven.
