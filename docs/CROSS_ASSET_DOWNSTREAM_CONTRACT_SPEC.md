# Vanguard Cross-Asset Downstream Contract Spec

Status: draft for QA implementation

Scope: Context Layer -> V5.1 Tradeability -> V5.2 Selection -> V6 Health -> V6 Risk Filters

Do not treat this as a model-training spec. This is the downstream routing contract that lets any asset-class model plug into the same decision pipeline without redefining V5 every time.

## Core Decision

Build one generic downstream contract now, with asset-specific adapters.

Implementation order:

1. Crypto adapter first, because crypto is available on weekends and can validate the downstream path while forex is closed.
2. Forex remains the primary production target and validates when the market opens.
3. Equity, metals, index, and energy get contract definitions now, but implementation should wait until their market sessions and source data can prove the path.

Reason:

If we only build crypto as a one-off validation bridge, we recreate the same hardcoded mess. If we fully build all instruments before testing one end to end, we multiply unknowns. The middle point is a shared schema plus thin asset-class adapters.

## Stage Ownership

V4B owns alpha only.

Outputs:

- `cycle_ts_utc`
- `symbol`
- `asset_class`
- `model_id`
- `feature_count`
- `predicted_return`
- `direction`
- `scored_at`

V4B must not decide economic tradeability, risk approval, sizing, or execution readiness.

Context Layer owns live/source truth.

Outputs latest truth tables by asset class. It does not approve trades.

V5.1 owns economics and feasibility.

It answers: does the model prediction survive current asset economics and basic feasibility?

V5.2 owns selection memory and shortlisting.

It answers: among economically viable candidates, which rows should be selected, watched, held, or rejected?

V6 Health owns freshness and operational readiness.

It answers: are the required upstream artifacts fresh and route-ready?

V6 Risk Filters / Risky owns policy approval.

It answers: is this trade allowed under profile/risk rules?

## Shared Data Flow

```text
V1/V2/V3 -> V4B predictions
Context builders -> asset state tables
V5.1 -> vanguard_v5_tradeability
V5.2 -> vanguard_v5_selection + shortlist mirrors
V6 Health -> vanguard_v6_context_health
V6 Risk Filters -> vanguard_tradeable_portfolio
Executor / lifecycle -> actual execution and close state
Outcome tracker -> model V2 realized outcomes
```

## Shared Asset State Contract

Forex already has `vanguard_forex_pair_state`. For cross-asset support, do not overload that table forever. Add a shared asset-state abstraction and keep the forex table as the forex-specific implementation.

Recommended table:

`vanguard_asset_state`

Fields:

- `cycle_ts_utc`
- `profile_id`
- `symbol`
- `asset_class`
- `state_ts_utc`
- `quote_ts_utc`
- `source`
- `broker`
- `live_bid`
- `live_ask`
- `live_mid`
- `spread_price`
- `spread_pips`
- `spread_bps`
- `spread_ratio_vs_baseline`
- `liquidity_bucket`
- `session_bucket`
- `trade_allowed`
- `min_qty`
- `max_qty`
- `qty_step`
- `tick_size`
- `tick_value`
- `contract_size`
- `open_position_count`
- `same_symbol_open_count`
- `correlated_open_count`
- `exposure_json`
- `asset_state_json`
- `config_version`
- `created_at`

Asset-specific tables can still exist when needed:

- `vanguard_forex_pair_state`
- future `vanguard_crypto_symbol_state`
- future `vanguard_equity_symbol_state`
- future `vanguard_commodity_symbol_state`

But V5.1 should consume a normalized state object, not hardcoded forex-only columns.

## V5.1 Generic Contract

Inputs:

- latest V4B predictions for the cycle
- normalized asset state
- V5.1 config JSON

Outputs:

`vanguard_v5_tradeability`

Required fields:

- `cycle_ts_utc`
- `profile_id`
- `symbol`
- `asset_class`
- `direction`
- `predicted_return`
- `predicted_move_native`
- `predicted_move_pips`
- `predicted_move_bps`
- `cost_native`
- `cost_pips`
- `cost_bps`
- `after_cost_native`
- `after_cost_pips`
- `after_cost_bps`
- `economics_state`
- `v5_action_prelim`
- `reasons_json`
- `state_ref`
- `context_snapshot_ref`
- `metrics_json`
- `config_version`

Allowed `economics_state`:

- `PASS`
- `WEAK`
- `FAIL`

V5.1 does not select. V5.1 does not size. V5.1 does not call broker APIs.

## V5.2 Generic Contract

Inputs:

- V5.1 rows
- recent V5.1/V5.2/V4B history
- selection config JSON
- live follow-through fields if available

Outputs:

`vanguard_v5_selection`

Required fields:

- `cycle_ts_utc`
- `profile_id`
- `symbol`
- `asset_class`
- `direction`
- `selected`
- `selection_rank`
- `selection_state`
- `selection_reason`
- `route_tier`
- `display_label`
- `v5_action`
- `direction_streak`
- `top_rank_streak`
- `strong_prediction_streak`
- `same_bucket_streak`
- `flip_count_window`
- `live_followthrough_pips`
- `live_followthrough_state`
- `flags_json`
- `metrics_json`
- `config_version`

Allowed selection states:

- `SELECTED`
- `WATCHLIST`
- `HOLD`
- `REJECTED`

Allowed flags:

- `ECONOMICS_PASS`
- `ECONOMICS_WEAK`
- `ECONOMICS_FAIL`
- `NEW_ENTRY`
- `STREAK_N`
- `FLIP_RISK`

Route tiers are annotations only:

- `TIER_1`
- `TIER_2`
- `NONE`

V5.2 selection order remains model-first:

1. Gate to `ECONOMICS_PASS` for routeable candidates.
2. Sort by absolute `predicted_return`.
3. Use after-cost value only as a tie-breaker.
4. Apply configured top-K limits.
5. Annotate tier, streak, flip risk, and follow-through.

Do not let route tier override model rank until outcome data proves that it should.

## Crypto Adapter

Purpose:

Validate downstream mechanics when forex is closed.

Crypto V4B:

- Current QA model exists and can be used as a mechanical test model.
- Active artifact: `lgbm_crypto_regressor_v1.pkl`
- Current metadata: `vanguard_crypto_model_meta.json`
- Current feature count: 29
- Current model quality is weak, so this is not production trading approval.

Crypto Context / Asset State:

Required fields:

- `live_bid`
- `live_ask`
- `live_mid`
- `spread_bps`
- `spread_ratio_vs_baseline`
- `quote_ts_utc`
- `source`
- `liquidity_bucket`
- `session_bucket = 24x7`
- `trade_allowed`
- `min_qty`
- `max_qty`
- `qty_step`
- `tick_size`
- `open_position_count`
- `same_symbol_open_count`
- `correlated_open_count`

Likely sources:

- Twelve Data for market data during QA validation.
- Broker/executor source later for executable crypto truth if crypto execution is enabled.

Crypto V5.1 economics:

- Prefer bps over pips.
- `predicted_move_bps = abs(predicted_return) * 10000`
- `cost_bps = spread_bps + configured_slippage_bps + configured_commission_bps`
- `after_cost_bps = predicted_move_bps - cost_bps`

Crypto V5.2:

- Same generic selection semantics.
- Model-first rank.
- Tier annotation only.

Crypto V6 Health:

Required context freshness:

- quote freshness
- asset-state freshness
- predictions freshness
- V5.1 freshness
- V5.2 freshness

Not required for weekend mechanical validation:

- broker account state
- live execution readiness

Crypto Risky/V6 Risk:

For validation mode:

- block actual execution
- allow shortlist/risk rows to be generated as paper/hold

For production mode later:

- require broker tradability
- require symbol specs
- require account/free-margin truth
- enforce max crypto exposure
- enforce min notional and quantity step

## Forex Adapter

Forex is the production target.

Forex State:

Existing table:

- `vanguard_forex_pair_state`

Required fields:

- `base_currency`
- `quote_currency`
- `usd_side`
- `spread_pips`
- `spread_ratio_vs_baseline`
- `session_bucket`
- `liquidity_bucket`
- `currency_exposure_json`
- `open_exposure_overlap_json`
- `usd_concentration`
- `same_currency_open_count`
- `correlated_open_count`
- `live_mid`
- `quote_ts_utc`
- `context_ts_utc`
- `source`
- `config_version`

Forex V5.1 economics:

- Use pips.
- `predicted_move_pips = abs(predicted_return) / pip_size`
- `cost_pips = spread_pips + slippage_buffer_pips + commission_pips`
- `after_cost_pips = predicted_move_pips - cost_pips`

Forex V5.2:

- Same generic selection semantics.
- Model-first rank.
- Streak/flip/follow-through as evidence only.

Forex V6 Health:

Required:

- quote freshness
- account freshness
- positions freshness
- orders freshness
- symbol specs freshness
- context daemon heartbeat

Forex Risky/V6 Risk:

Must enforce:

- prop-firm drawdown constraints
- floating loss constraints
- max open positions
- duplicate symbol rules
- spread sanity
- min stop distance
- session/liquidity rules
- correlation/currency exposure rules
- closing buffer

## Equity Adapter

Equity should not be forced into forex pips logic.

Equity State:

Required fields:

- `live_bid`
- `live_ask`
- `live_mid`
- `spread_bps`
- `quote_ts_utc`
- `session_bucket`
- `liquidity_bucket`
- `trade_allowed`
- `halt_status`
- `shortable`
- `borrow_available`
- `adv_bucket`
- `gap_pct`
- `sector`
- `benchmark_state`
- `open_position_count`
- `same_symbol_open_count`
- `sector_exposure_json`

Equity V5.1 economics:

- Use bps and dollars.
- `predicted_move_bps = abs(predicted_return) * 10000`
- `cost_bps = spread_bps + slippage_bps + commission_bps`
- `after_cost_bps = predicted_move_bps - cost_bps`

Equity-specific blockers:

- halt/restricted status
- shortability for shorts
- spread too wide
- low liquidity
- near close if profile disallows
- earnings proximity later, not required for first pass

Equity V6 Health:

Required:

- quote freshness
- market session status
- source health
- account context if execution enabled
- positions/orders if execution enabled

## Metals / Energy / Index Adapter

These are contract-like instruments and should be handled as commodity/index CFDs unless the broker source proves otherwise.

State fields:

- `live_bid`
- `live_ask`
- `live_mid`
- `spread_bps`
- `spread_points`
- `quote_ts_utc`
- `session_bucket`
- `liquidity_bucket`
- `trade_allowed`
- `tick_size`
- `tick_value`
- `contract_size`
- `margin_rate`
- `open_position_count`
- `correlated_open_count`
- `usd_exposure`
- `commodity_group`

V5.1 economics:

- Use bps for normalized comparison.
- Also store native points.
- `predicted_move_bps = abs(predicted_return) * 10000`
- `cost_bps = spread_bps + slippage_bps + commission_bps`
- `after_cost_bps = predicted_move_bps - cost_bps`

Risk-specific blockers:

- session restrictions
- wide spreads around roll/illiquid hours
- correlated exposure between metals/energy/index groups
- margin stress

## V6 Health Contract

V6 Health should be account/profile configurable by JSON.

It should check freshness and completeness, not trading profitability.

Inputs:

- V4B prediction timestamp
- asset-state timestamp
- V5.1 timestamp
- V5.2 timestamp
- account timestamp where required
- positions/orders timestamp where required
- symbol specs timestamp where required
- source health rows

Outputs:

- `READY`
- `HOLD`
- `BLOCK`
- `ERROR`

Example behavior:

- stale quote -> `HOLD` if temporary, `BLOCK` if past hard threshold
- missing symbol specs -> `BLOCK`
- dead daemon -> `ERROR`
- selected row too old -> `BLOCK`
- missing V5.1/V5.2 -> `ERROR`

## Risky / V6 Risk Filter Contract

Risky logic should be merged as check modules or rule functions inside Vanguard's V6 risk/policy path.

Do not merge Risky's FastAPI service complexity.

Use:

- `Risky/config/risk_rules.json` semantics
- `Risky/data/spread_baselines.json`
- check logic from `Risky/risky/checks/`

But feed it from Vanguard DB truth:

- V5.2 selected candidate
- asset state
- account context
- positions/orders context
- runtime profile config

Risky/V6 should enforce:

- drawdown constraints
- floating loss constraints
- duplicate symbol
- max positions
- max exposure
- spread sanity
- min SL distance
- session liquidity
- correlation caps
- budget/free-margin
- closing buffer

## Config Shape

Recommended files:

- `config/vanguard_asset_state.json`
- `config/vanguard_v5_tradeability.json`
- `config/vanguard_v5_selection.json`
- `config/vanguard_v6_health.json`
- `config/vanguard_runtime.json`

Asset-specific sections should exist under each:

```json
{
  "asset_classes": {
    "forex": {},
    "crypto": {},
    "equity": {},
    "metal": {},
    "energy": {},
    "index": {}
  }
}
```

No hidden hardcoded thresholds in stage code unless they are explicit defaults with config override.

## Implementation Phases

Phase 1: Crypto validation bridge

- Add normalized crypto asset-state builder.
- Let V5.1 consume generic asset state instead of forex-only state.
- Keep forex pair-state path intact.
- Run crypto predictions through V5.1/V5.2.
- Mirror selected rows to shortlist tables in non-execution validation mode.
- Confirm V6 Health and V6 Risk can consume selected rows without actual execution.

Phase 2: Forex production validation

- Run live forex market.
- Confirm DWX/TwelveData/source health.
- Confirm pair-state freshness.
- Confirm V5.1/V5.2 produce sane selected rows.
- Confirm V6 Health/Risk emits route-ready or blocked rows with clear reasons.

Phase 3: Risky/V6 merge

- Port Risky checks into Vanguard policy flow.
- Add JSON profile config.
- Add tests per check.
- Keep V5 untouched.

Phase 4: Equity/metals/index adapters

- Implement asset-state builders.
- Add V5.1 economics units.
- Add V6 health requirements.
- Add Risky rules.
- Validate in market hours.

Phase 5: new models

- Only train or wire new asset models after the downstream contract is proven on at least one live asset class.

## Acceptance Criteria For Crypto Weekend Validation

Pass if:

- V1 writes fresh crypto bars.
- V2 produces at least one crypto survivor, or validation mode can force a bounded crypto pass without execution.
- V3 writes factor rows for crypto.
- V4B writes crypto predictions with `model_id` and `feature_count`.
- Crypto asset state rows exist.
- V5.1 writes `PASS | WEAK | FAIL`.
- V5.2 selects only `PASS`.
- shortlist mirrors contain only selected V5.2 rows.
- V6 Health writes explicit `READY | HOLD | BLOCK | ERROR`.
- V6 Risk writes tradeable rows or blocked rows with reasons.
- no actual execution occurs.

Fail if:

- V5.1/V5.2 bypass context.
- selection order is no longer model-first.
- route tier changes rank.
- stale context is treated as route-ready.
- actual execution can happen from the validation bridge.

