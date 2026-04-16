<proposed_plan>
# FTMO Forex Position Manager Spec

## Summary
Build a new **position manager** for live/open trades. It must be **post-entry only** and **config-driven**. Risky keeps owning new-entry gating, sizing, and initial SL/TP. The position manager owns hold/close/modify decisions after a trade is already open.

This first version is:
- **forex only**
- for the `raw_return_6bar` model family
- for **FTMO local MT5 / DWX**
- with **broker-native SL/TP always on**
- with **one extension only** from `30m -> 60m`
- with **no scale-in**
- with **no TP extension**

The manager must be audit-friendly, append-only in review history, and safe against repeated close/modify submits.
It must also be resilient to sparse-cycle signal gaps and data-quality degradation observed in recent QA runs.

## Architecture

### Responsibility split
**Risky keeps:**
- entry approval / rejection
- size, SL, TP at entry
- duplicate symbol block
- max-open-position checks for **new entries**
- drawdown / account safety checks for opening trades
- sub-1R entry rejection
- V6 route readiness for new trades

**Position manager owns:**
- monitoring already-open trades
- hold vs close vs modify-SL decisions
- 10m / 30m / 60m lifecycle logic
- flip persistence checks
- extension logic
- breakeven moves
- health/context degradation handling for open trades
- manager-generated exit reason codes

**Broker-native SL/TP remains underneath both** and has highest priority.

### Execution boundary
Use existing DWX bridge write actions only:
- `close_position()`
- `modify_position()`

Do not put strategy logic into the executor.

### Existing components to reuse
- [dwx_executor.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/executors/dwx_executor.py)
- [trade_journal.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/execution/trade_journal.py)
- [reconciler.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/execution/reconciler.py)
- [open_positions_writer.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/execution/open_positions_writer.py)
- [vanguard_context_positions_latest](/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db)
- [vanguard_context_account_latest](/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db)
- [vanguard_v5_tradeability](/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db)
- [vanguard_v5_selection](/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db)
- [vanguard_v6_context_health](/Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db)

## Data Model

### 1. Journal additions
At approval/open time, persist these on the trade journal row so lifecycle decisions stay bound to the original trade contract:
- `policy_id`
- `model_id`
- `model_family`
- `opened_cycle_ts_utc`
- `original_entry_ref_price`
- `original_stop_price`
- `original_tp_price`
- `original_risk_dollars`
- `original_r_distance_native`
- `original_rr_multiple`
- `original_r_multiple_target`

This prevents runtime-config drift from changing open-trade behavior.
The manager must always prefer these persisted trade-bound fields over current runtime defaults.

### 2. New append-only lifecycle review table
Add a new append-only table:
- `vanguard_position_lifecycle`

Required columns:
- `review_id INTEGER PRIMARY KEY AUTOINCREMENT`
- `profile_id`
- `trade_id`
- `broker_position_id`
- `symbol`
- `side`
- `policy_id`
- `model_id`
- `model_family`
- `opened_cycle_ts_utc`
- `opened_at_utc`
- `reviewed_at_utc`
- `holding_minutes`
- `bars_elapsed`
- `lifecycle_phase`
- `review_action`
- `review_reason_codes_json`
- `extension_granted_count`
- `breakeven_moved`
- `last_action_type`
- `last_action_submitted_at_utc`
- `last_action_status`
- `last_action_idempotency_key`
- `thesis_snapshot_json`
- `same_side_signal_snapshot_json`
- `opposite_side_signal_snapshot_json`
- `latest_state_snapshot_json`
- `latest_health_snapshot_json`
- `latest_position_snapshot_json`
- `manager_note`

This table records **every review**, even when action is `HOLD`.

Also add a latest view:
- `vanguard_position_lifecycle_latest`

Rationale from runtime:
- every review must be inspectable later
- missing-signal or stale-context HOLD reviews are first-class evidence, not noise
- overwriting the latest state would hide the exact moment a trade stopped being extendable

### 3. Close attribution fields
The journal and/or lifecycle records must distinguish broker exits from manager exits.

Add fields:
- `broker_close_reason`
- `manager_close_reason`
- `close_detected_via`
  - `broker`
  - `manager`
  - `reconciliation`
- `close_order_id`
- `close_fill_price`
- `close_fill_ts_utc`

Broker-native `SL/TP` closures must never be relabeled as manager exits.
Manager-initiated closes and broker-native closes must remain distinguishable in audit and reconciliation trails.

## Policy Model

### Position manager config
Add to runtime JSON:
- `position_manager.enabled`
- `position_manager.poll_interval_seconds`
- `position_manager.default_policy_by_asset_class`
- `position_manager.policies`

First policy:
- `raw_return_6bar_forex_v1`

Example shape:
```json
"position_manager": {
  "enabled": true,
  "poll_interval_seconds": 30,
  "default_policy_by_asset_class": {
    "forex": "raw_return_6bar_forex_v1"
  },
  "policies": {
    "raw_return_6bar_forex_v1": {
      "asset_class": "forex",
      "model_family": "raw_return_6bar",
      "model_horizon_bars": 6,
      "bar_interval_minutes": 5,
      "initial_flip_grace_bars": 2,
      "early_exit_min_bars": 2,
      "early_exit_requires_negative_pnl": true,
      "early_exit_requires_two_consecutive_opposite_cycles": true,
      "early_exit_requires_same_side_v51_pass_false": true,
      "early_exit_requires_same_side_selection_false": true,
      "early_exit_skip_if_near_tp": true,
      "near_tp_threshold_rr": 0.8,
      "review_at_bars": 6,
      "close_at_30m_if_profit_below_rr": 0.25,
      "extend_to_bars": 12,
      "max_extensions": 1,
      "extension_requires_profit": true,
      "extension_min_rr": 0.5,
      "extension_requires_same_side_signal": true,
      "extension_requires_v51_pass": true,
      "extension_requires_v52_selected": true,
      "extension_allowed_followthrough_states": ["ALIGNED", "FLAT"],
      "extension_disallowed_post_entry_health_codes": ["FATAL_POSITION_MISSING", "BROKER_STATE_INVALID"],
      "breakeven_on_extension": true,
      "breakeven_offset_mode": "spread",
      "hard_close_bars": 12,
      "allow_scale_in": false,
      "allow_tp_extension": false
    }
  }
}
```

## Signal, Health, and R Semantics

### 1. Query semantics for current signal
Never use “latest signal” loosely.

For each open trade, the manager must resolve:
- **same-side signal state**
  - latest row for same `profile_id`, same `symbol`, same `direction`
- **opposite-side signal state**
  - latest row for same `profile_id`, same `symbol`, opposite `direction`

Use both:
- same-side for hold/extend support
- opposite-side for flip persistence

Do not use a single generic latest row.

If same-side or opposite-side V5.1 / V5 rows are missing at review time:
- classify the review as `SIGNAL_UNAVAILABLE`
- default to `HOLD`
- do not auto-close solely because one or both rows are absent
- only override that default for:
  - broker-native closure already detected
  - hard max-hold reached
  - fatal post-entry health
  - fatal post-entry risk

### 2. R definition
All lifecycle `R` logic must use the **original approved trade geometry**, not the current modified stop.

Define:
- `R = original stop distance at entry`

Use persisted originals to compute:
- current PnL in `R`
- distance-to-TP in `R`
- near-TP checks
- 0.25R and 0.5R thresholds

Never recompute `R` from:
- current stop after breakeven move
- current spread
- current trailing logic

### 3. Health semantics
Do not use `ready_to_route = false` as a generic close trigger.

Split health into:
- **post-entry fatal health**
  - position truth broken
  - broker state invalid
  - account state unusable
- **non-fatal degraded health**
  - stale quote
  - missing extension inputs
  - temporary health hold

Rules:
- fatal health may force close
- degraded health may deny extension / deny SL modification
- degraded health alone should not panic-close an otherwise normal open trade

### 4. Position truth precedence
For open-trade lifecycle decisions, use:
1. `vanguard_context_positions_latest`
2. `vanguard_trade_journal` original contract fields
3. reconciliation for divergence classification and repair

Missing or stale broker position truth should default to `HOLD`, not forced close, unless the condition becomes repeated or is confirmed fatal.

### 5. Clock anchor semantics
- `bars_elapsed` is computed from `opened_cycle_ts_utc`
- `holding_minutes` is computed from `opened_at_utc`
- lifecycle horizon decisions (`30m`, `60m`) use `bars_elapsed`, not broker fill latency

This keeps the lifecycle aligned with model horizon while preserving real-time operational reporting.

## Forex Exit Policy

### Priority order
1. `STOP_LOSS`
2. `TAKE_PROFIT`
3. manager-controlled exits below

### Before 10 minutes / first 2 bars
- ignore single signal flips
- no early close from one-cycle noise
- only allow hard health/risk kill

### Early adverse exit before 30m
Close early only if **all** are true:
- open for at least `10 minutes`
- opposite signal persists for `2 consecutive cycles`
- live PnL is negative
- trade is not near TP
- same-side V5.1 is no longer `PASS`
- same-side V5 selection no longer supports the side

Exit reason:
- `EARLY_FLIP_EXIT`

### Decision at 30m / 6 bars
Close at 30m if **any** are true:
- trade is flat or negative
- trade is `< 0.25R`
- same-side signal no longer persists
- same-side V5.1 is not `PASS`
- same-side V5 selection no longer supports the side
- live follow-through is `AGAINST`
- post-entry fatal health is true
- post-entry risk exit is triggered

Exit reasons:
- `TIME_STOP_30M`
- `HEALTH_EXIT`
- `RISK_EXIT`

`RISK_EXIT` is allowed only for:
- floating loss hard breach
- trailing drawdown hard breach
- absolute max loss hard breach
- forced weekend / closing-buffer liquidation
- explicit operator hard kill

### Extension from 30m to 60m
Extend only if **all** are true:
- trade is profitable
- same-side signal persists
- same-side V5.1 remains `PASS`
- same-side V5 selection remains `SELECTED` / high-quality
- live follow-through is `ALIGNED` or `FLAT`
- trade is `>= 0.5R` or materially near TP
- post-entry health remains non-fatal and good enough for extension

On extension:
- move SL to breakeven or breakeven + spread
- do not add size
- keep TP unchanged
- allow only one extension

If, during extension:
- opposite signal persists 2 cycles
- trade is not near TP
then close early.

### Hard max hold
- absolute hard close at `60 minutes`
- reason:
  - `EXTENDED_HOLD_60M`

## Idempotency and Safety

### Required action locking
The manager must be safe under repeated polling.

Rules:
- no second `CLOSE` submit while previous close action is pending
- no repeated breakeven move once `breakeven_moved = true`
- no repeated modify for same intended SL value within same lifecycle state
- action submissions must use an idempotency key built from:
  - `trade_id`
  - `broker_position_id`
  - `review_action`
  - `target_sl` or `target_close_reason`
  - `reviewed_cycle`

Retry/backoff rules:
- no repeated close/modify submit within the same poll
- failed actions retry only after cooldown
- enforce max retry count per action type
- after retry exhaustion, persist `ACTION_RETRY_EXHAUSTED`
- after exhaustion, stop resubmitting automatically until new operator intervention or a materially new lifecycle state

### Action outcomes
Persist action lifecycle:
- `PENDING_SUBMIT`
- `SUBMITTED`
- `CONFIRMED`
- `FAILED`
- `SKIPPED_ALREADY_APPLIED`

## Manager Loop

### Inputs per review
For each open position:
- broker position truth
- journal row
- same-side V5.1 row
- opposite-side V5.1 row
- same-side V5 selection row
- opposite-side V5 selection row
- post-entry health snapshot
- current pair state / symbol state
- current account truth

### Outputs per review
For each review:
- append one lifecycle row
- if action needed:
  - call `modify_position()` or `close_position()`
  - write action result
  - update journal if confirmed
- otherwise:
  - `HOLD`

## Test Plan

### Unit
- ignore first single flip during first 2 bars
- early exit only fires when all adverse conditions are true
- 30m close fires on:
  - flat/negative
  - `< 0.25R`
  - same-side V5.1 not PASS
  - same-side V5 no longer selected
  - follow-through AGAINST
  - fatal health
- extension fires only when all extension conditions are true
- extension moves SL to breakeven exactly once
- hard close fires at 60m
- idempotency prevents duplicate close/modify actions

### Integration
- manager reads current broker truth and journal correctly
- same-side and opposite-side V5 rows are resolved separately
- broker-native SL/TP close is recorded as broker reason, not manager reason
- reconciliation still repairs journal if broker closes first
- lifecycle review history is append-only
- latest lifecycle view resolves correctly

### Acceptance criteria
- open a live/demo FTMO forex trade
- manager records reviews every poll
- early adverse flip exit works
- 30m review closes weak trades correctly
- 30m extension works once for strong trades
- 60m hard close works
- operator can inspect lifecycle state and reason trail from DB without reading raw logs

## Runtime Findings Incorporated

Recent QA runs surfaced three concrete lessons that the manager must respect:

1. **Sparse or degraded cycles are real**
- some cycles had missing or thin same-side/opposite-side signal rows
- the manager must treat missing signal as `SIGNAL_UNAVAILABLE -> HOLD`, not as a synthetic flip

2. **Entry suppressors and post-entry suppressors are different**
- after the forex V5.1 spread redesign, downstream suppression shifted toward:
  - `BLOCKED_POSITION_LIMIT`
  - `BLOCKED_SUB_1R_AFTER_COST`
- the manager should not inherit pre-entry Risky logic blindly; it needs narrow post-entry rules only

3. **Data-health degradation is often non-fatal**
- V2 / context degradation can temporarily make rows unavailable or stale
- that is sufficient reason to deny extension
- it is not sufficient reason by itself to panic-close a healthy open trade

## Defaults and Assumptions
- first implementation is **forex only**
- crypto lifecycle rules are out of scope for v1
- V5 selection is the current source of “V5.2 still likes it” truth
- broker-native SL/TP is mandatory and always first-priority
- manager policy is bound to the original trade and does not drift with later runtime-config changes
</proposed_plan>
