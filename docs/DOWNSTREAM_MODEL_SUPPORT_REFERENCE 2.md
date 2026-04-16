# Downstream Model Support Reference

This note is the working reference for the downstream system changes made around the new model integration and QA rollout.

It focuses on the active downstream stack after scoring:
- context layer
- pair/state layer
- V5.1 tradeability
- V5.2 selection
- V6 / Risky approvals
- MT5 DWX bridge and executor
- trade journal / reconciliation
- position manager scaffold
- operator visibility via Telegram and DB tables

This is not a full repo changelog. It is the downstream working set.

## 1. Top-Level Runtime / Wiring

### Main runtime config
- `config/vanguard_runtime.json`
  - profile activation
  - market-data source split
  - live vs manual routing
  - Telegram behavior
  - context-source bindings
  - position-manager policy block

### Key runtime helpers
- `vanguard/config/runtime_config.py`
- `vanguard/accounts/runtime_config.py`
- `vanguard/accounts/runtime_universe.py`
- `vanguard/accounts/account_state.py`
- `vanguard/helpers/symbol_identity.py`

### Important current runtime behavior
- forex bars can be sourced from Twelve Data
- crypto bars can be sourced from MT5 local / DWX
- live routing is restricted to forex
- position manager is scaffolded but disabled by default

## 2. Context Layer

### Context ingest / broker truth
- `scripts/dwx_live_context_daemon.py`
  - polls MT5 / DWX live broker truth into context tables
- `vanguard/data_adapters/mt5_dwx_adapter.py`
  - MT5/DWX historic + quote adapter
- `vanguard/context_health.py`
  - entry-side context health and route readiness checks

### Context tables to inspect
- `vanguard_context_quote_latest`
- `vanguard_context_account_latest`
- `vanguard_context_positions_latest`
- `vanguard_context_orders_latest`
- `vanguard_context_symbol_specs`
- `vanguard_context_ingest_runs`

### What these tables answer
- are live quotes fresh?
- is broker account truth present?
- are open positions mirrored?
- are specs available?
- is source freshness degraded?

## 3. Pair State / Symbol State Layer

### Config
- `config/vanguard_forex_pair_state.json`
- `config/vanguard_crypto_context.json`

### Build scripts
- `scripts/build_forex_pair_state.py`
- `scripts/build_crypto_symbol_state.py`

### State tables
- `vanguard_forex_pair_state`
- `vanguard_crypto_symbol_state`

### What these layers do
- forex:
  - spread regime
  - spread ratio vs baseline
  - liquidity bucket
  - correlation / overlap fields
  - currency exposure overlap
- crypto:
  - symbol-level context and route-readiness support

## 4. V5.1 Tradeability Layer

### Config
- `config/vanguard_v5_tradeability.json`

### Stage / implementation
- `stages/vanguard_selection_tradeability.py`
- `vanguard/selection/tradeability.py`
- `scripts/run_v5_tradeability.py`

### Table
- `vanguard_v5_tradeability`

### What this layer writes
- predicted move
- spread / cost / after-cost economics
- economics state
- fail / weak / pass behavior
- route tier
- reasons JSON
- pair-state and context snapshot references

### Important recent change
- forex spread gating was changed away from a hard universal `2.5 pip` fail
- forex now relies on dynamic spread-ratio behavior instead of a blunt absolute pip cap

## 5. V5.2 Selection Layer

### Config
- `config/vanguard_v5_selection.json`

### Implementation
- `vanguard/selection/shortlisting.py`
- `scripts/run_v5_selection.py`

### Table
- `vanguard_v5_selection`

### What this layer writes
- selected vs not selected
- selection rank
- route tier
- selection state
- selection reason
- cadence / streak memory
- live follow-through fields

### Important recent change
- cadence memory was fixed so consecutive selected rows at real loop cadence are counted correctly
- stale/failure/flat rows should no longer present fake operator conviction the way they did before

## 6. V6 / Risky Approval Layer

### Stage
- `stages/vanguard_risk_filters.py`

### Core risk modules
- `vanguard/risk/risky_policy_adapter.py`
- `vanguard/risk/policy_engine.py`
- `vanguard/risk/risky_sizing.py`
- `vanguard/risk/position_limits.py`
- `vanguard/risk/reject_rules.py`
- `vanguard/risk/holding_rules.py`
- `vanguard/risk/side_controls.py`
- `vanguard/risk/drawdown_pause.py`
- `vanguard/risk/sizing_forex.py`
- `vanguard/risk/sizing_crypto.py`
- `vanguard/risk/sizing_equity.py`
- `vanguard/risk/account_state.py`
- `vanguard/risk/position_lifecycle.py`

### Approval table
- `vanguard_tradeable_portfolio`

### What this layer answers
- which V5 rows are actually approved to trade?
- what size / stop / TP / risk dollars were chosen?
- what was blocked and why?
- position-limit vs sub-1R vs health-based rejection

## 7. Orchestrator / Telegram / Session Logic

### Main orchestrator
- `stages/vanguard_orchestrator.py`

### Related supporting stage files
- `stages/vanguard_prefilter.py`
- `stages/vanguard_factor_engine.py`
- `stages/vanguard_scorer.py`
- `stages/vanguard_selection_simple.py`

### Important recent downstream behavior
- stage-based Telegram messaging
- action vs diagnostics split
- account snapshot in Telegram
- context/state diagnostic logs
- source-split handling for forex vs crypto market data
- Sunday-evening / overnight forex session resolution fix in runtime universe handling

## 8. DWX / MT5 Execution Boundary

### Bridge / adapter
- `vanguard/data_adapters/mt5_dwx_adapter.py`

### Executor
- `vanguard/executors/dwx_executor.py`

### Related execution bridge helpers
- `vanguard/executors/mt5_executor.py`
- `vanguard/api/trade_desk.py`

### What this layer owns
- place order
- modify SL/TP
- close position
- FTMO MT5 local execution path
- broker-side fill / ticket / failure propagation

## 9. Trade Lifecycle / Reconciliation / Open Position Tracking

### Journal and truth sync
- `vanguard/execution/trade_journal.py`
- `vanguard/execution/open_positions_writer.py`
- `vanguard/execution/reconciler.py`
- `vanguard/execution/shadow_log.py`

### Lifecycle code currently in repo
- `vanguard/execution/lifecycle_daemon.py`
- `vanguard/execution/auto_close.py`

### Current assessment
- journal / reconciliation pieces are real and reusable
- existing lifecycle / auto-close logic is not the final production solution for FTMO forex automation
- post-entry manager work should build on these components, not replace them blindly

### Important tables
- `vanguard_trade_journal`
- `vanguard_open_positions`
- `vanguard_reconciliation_log`
- `vanguard_shadow_execution_log`

## 10. Position Manager Scaffold

### Spec
- `Position_manager_spec.md`

### Code scaffold
- `vanguard/execution/position_manager.py`

### Current status
- schema + review loop scaffold exists
- policy binding and lifecycle review scaffolding exist
- manager is not the active production exit system yet
- broker-action wiring is intentionally not the active closing engine yet

## 11. Data Adapters / Source Modes

### Source adapters
- `vanguard/data_adapters/twelvedata_adapter.py`
- `vanguard/data_adapters/ibkr_adapter.py`
- `vanguard/data_adapters/alpaca_adapter.py`
- `vanguard/data_adapters/mt5_dwx_adapter.py`

### Useful reference docs
- `docs/CODEX_V4B_V7_CURRENT_PATH_AND_SOURCE_MODES.md`
- `docs/CROSS_ASSET_DOWNSTREAM_CONTRACT_SPEC.md`
- `docs/VANGUARD_ORCHESTRATOR_ARCHITECTURE_MAP.md`

## 12. Important DB Tables for Model Results

### Raw / model-output side
- `vanguard_bars_1m`
- `vanguard_bars_5m`
- `vanguard_features`
- `vanguard_factor_matrix`
- `vanguard_predictions`
- `vanguard_health`

### Post-model downstream side
- `vanguard_forex_pair_state`
- `vanguard_crypto_symbol_state`
- `vanguard_v5_tradeability`
- `vanguard_v5_selection`
- `vanguard_v6_context_health`
- `vanguard_tradeable_portfolio`

### Broker / execution / lifecycle side
- `vanguard_context_quote_latest`
- `vanguard_context_account_latest`
- `vanguard_context_positions_latest`
- `vanguard_trade_journal`
- `vanguard_open_positions`
- `vanguard_reconciliation_log`
- `vanguard_execution_log`
- `vanguard_shadow_execution_log`

## 13. Best Tables To Inspect During QA

### To inspect current model output
- `vanguard_v5_tradeability`
  - economics pass/fail
  - after-cost move
  - fail reasons
- `vanguard_v5_selection`
  - selected rows
  - rank / route tier
  - cadence and follow-through

### To inspect approvals / rejects
- `vanguard_tradeable_portfolio`
  - approved vs blocked
  - rejection reason
  - stop / target / qty / risk dollars

### To inspect execution / live-trade truth
- `vanguard_trade_journal`
  - expected entry / stop / target
  - fill price
  - close reason
  - realized PnL / realized R
- `vanguard_context_positions_latest`
  - broker open positions

### To inspect data-health issues
- `vanguard_context_quote_latest`
- `vanguard_forex_pair_state`
- `vanguard_v6_context_health`
- `vanguard_source_health`

## 14. Scripts / Control Surface

### Main operational scripts
- `scripts/dwx_live_context_daemon.py`
- `scripts/build_forex_pair_state.py`
- `scripts/build_crypto_symbol_state.py`
- `scripts/run_v5_tradeability.py`
- `scripts/run_v5_selection.py`
- `scripts/start_lifecycle_daemon.sh`
- `scripts/stop_lifecycle_daemon.sh`
- `scripts/reconcile_once.py`
- `scripts/emergency_stop.sh`

### Why they matter
- context truth refresh
- state rebuild
- isolated stage reruns
- lifecycle daemon control
- emergency stop / reconciliation utilities

## 15. Tests Added / Updated Around This Path

### High-signal tests
- `tests/test_forex_pair_state.py`
- `tests/test_context_health_crypto.py`
- `tests/test_v5_tradeability.py`
- `tests/test_v5_shortlisting.py`
- `tests/test_risky_policy_adapter.py`
- `tests/test_dwx_live_context_daemon.py`
- `tests/test_position_manager.py`
- `tests/test_vanguard_orchestrator.py`

### Why these matter
- pair-state contract
- context-health behavior
- V5.1 gating behavior
- V5.2 cadence / selection memory
- Risky approval / rejection mechanics
- DWX context ingest
- position manager scaffold behavior
- orchestrator integration

## 16. Practical Reading Order

If you need to debug the downstream stack quickly, use this order:

1. `config/vanguard_runtime.json`
2. `stages/vanguard_orchestrator.py`
3. `vanguard/context_health.py`
4. `scripts/build_forex_pair_state.py`
5. `vanguard/selection/tradeability.py`
6. `vanguard/selection/shortlisting.py`
7. `stages/vanguard_risk_filters.py`
8. `vanguard/executors/dwx_executor.py`
9. `vanguard/execution/trade_journal.py`
10. `vanguard/execution/reconciler.py`
11. `Position_manager_spec.md`
12. `vanguard/execution/position_manager.py`

## 17. Current Reality Check

The current downstream stack is good enough for:
- QA
- manual operator review
- replay / audit analysis
- supervised paper automation experiments

The main open gaps before production live automation are:
- SL / TP geometry redesign for forex
- stronger pair / regime filtering
- final position-manager activation
- more trustworthy operator UX / reporting

## 18. Discussion Notes: Opus Report Assessment

This section records the later cross-check against the Opus analysis so the active issues are separated from stale diagnoses.

### 18.1 Confirmed current issue
- **Forex model-horizon exit geometry is using an effective 3-pip stop floor in many approvals**
  - active code path:
    - `vanguard/risk/risky_sizing.py`
    - `_model_exit_geometry()`
  - current forex stop distance uses:
    - `min_stop_floor_pips`
    - `min_spread_multiple`
    - `min_atr_stop_multiple`
  - active risk policy source:
    - `~/SS/Risky/config/risk_rules.json`
  - current Risky defaults include:
    - `min_stop_floor_pips = 3.0`
    - `min_atr_stop_multiple = 0.35`
  - DB confirmation from approved forex rows showed many approvals at exactly `3.0` pip stops:
    - `EURGBP LONG`
    - `USDCHF LONG`
    - `EURJPY SHORT`
    - `USDCAD LONG`
    - `AUDUSD SHORT`
    - `GBPUSD SHORT`

Interpretation:
- this is a real downstream bug / miscalibration for a 6-bar / 30-minute model
- it likely explains a large share of immediate stop-outs
- the active fix belongs in `Risky/config/risk_rules.json`, not just `config/vanguard_runtime.json`

### 18.2 Confirmed but secondary issue
- **VP / feature integrity still needs verification**
  - LightGBM model artifacts do not store human-readable feature names
  - inference depends on exact feature order matching training order
  - this is a real model-integrity risk, but it is secondary to the current stop-geometry bug

### 18.3 What is no longer true in current state
- **The “all forex is blocked by stale Sunday-open context quotes” diagnosis is no longer true**
  - current `vanguard_context_quote_latest` rows for:
    - `EURGBP`
    - `EURJPY`
    - `EURUSD`
    - `USDCAD`
    - `USDCHF`
    show fresh `mt5_dwx` quotes with `source_status = OK`
  - current `vanguard_v5_tradeability` forex rows are now `PASS` with normal spreads and positive after-cost values
  - forex is no longer universally dying at V5.1 due to frozen Sunday-open spreads

Nuance:
- the standalone `dwx_live_context_daemon.py` process was not running at the moment of the later check
- but current quotes were still fresh in DB, which means the active pipeline had refreshed context through the orchestrator path
- so the “restart daemon and trading will work” conclusion described an earlier broken state, not the later validated one

### 18.4 Operational conclusion from the cross-check
- **Current primary blocker:** forex stop/TP geometry and pair quality
- **Earlier blocker that was real but has since been cleared:** stale context spreads killing V5.1
- **Ongoing investigation:** feature-order integrity / model-certainty issues
