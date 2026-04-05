# Vanguard Repo Audit

## 1. Files / docs reviewed

Top-level structure reviewed:
- `VANGUARD_AGENTS.md`
- `VANGUARD_ROADMAP.md`
- `config/`
- `data/`
- `docs/`
- `models/`
- `scripts/`
- `stages/`
- `tests/`
- `vanguard/`

Primary docs reviewed:
- `VANGUARD_AGENTS.md`
- `VANGUARD_ROADMAP.md`
- `docs/PROJECT_VANGUARD_HANDOFF.md`
- `docs/VANGUARD_BUILD_PLAN.md`
- `docs/VANGUARD_SUPPORTING_SPECS.md`
- `docs/VANGUARD_STAGE_V1_SPEC.md`
- `docs/VANGUARD_STAGE_V2_SPEC.md`
- `docs/VANGUARD_STAGE_V3_SPEC.md`
- `docs/VANGUARD_STAGE_V4A_SPEC.md`
- `docs/VANGUARD_STAGE_V4B_SPEC.md`
- `docs/VANGUARD_STAGE_V5_SPEC.md`
- `docs/VANGUARD_STAGE_V6_SPEC.md`
- `docs/VANGUARD_STAGE_V7_SPEC.md`
- `docs/VANGUARD_STAGE_V7_1_EXECUTOR_SPEC.md`

Implementation/runtime files reviewed:
- `stages/vanguard_cache.py`
- `stages/vanguard_prefilter.py`
- `stages/vanguard_factor_engine.py`
- `stages/vanguard_training_backfill_fast.py`
- `scripts/execute_daily_picks.py`
- `vanguard/helpers/db.py`
- `vanguard/helpers/clock.py`
- `vanguard/helpers/bars.py`
- `vanguard/data_adapters/alpaca_adapter.py`
- `vanguard/data_adapters/mt5_data_adapter.py`
- `vanguard/execution/bridge.py`
- `vanguard/execution/signalstack_adapter.py`
- `vanguard/execution/telegram_alerts.py`
- `vanguard/factors/price_location.py`
- `vanguard/factors/momentum.py`

State checks performed:
- repo tree and file inventory
- zero-byte file inventory
- SQLite schema/row-count inspection of `data/vanguard_universe.db`
- `python3 -m pytest -q` in repo root

## 2. Intended architecture from docs

The docs define Vanguard as a 7-stage, multi-asset intraday system:

`V1 Cache -> V2 Prefilter -> V3 Features -> V4A Backfill -> V4B ML -> V5 Selection -> V6 Risk -> V7 Orchestrator/Execution`

Key architectural expectations from the docs:
- separate isolated DB: `data/vanguard_universe.db`
- multi-asset from day 1: equities, forex, indices, metals, crypto, commodities, futures
- 5-minute intraday pipeline with session-aware logic
- 1-minute canonical bars with 5m and 1h derived bars
- 35-feature factor matrix in Stage 3
- canonical tables for training, models, shortlist, tradeable portfolio, portfolio state, execution log, session log
- config-driven runtime using multiple config JSON files
- executor-first build order in Phase 0, but with a clear path into full V1-V7

The roadmap/build plan also makes a stronger practical claim:
- current phase is Phase 0, executor bridge first
- executor should be reusable for Meridian + S1 immediately
- V1-V7 should follow afterward

## 3. Actual implementation state

### Built

The following are actually implemented and non-trivial:

1. Phase 0 executor bridge:
- `vanguard/execution/bridge.py`
- `vanguard/execution/signalstack_adapter.py`
- `vanguard/execution/telegram_alerts.py`
- `scripts/execute_daily_picks.py`

2. Early data pipeline:
- `stages/vanguard_cache.py`
- `stages/vanguard_prefilter.py`
- `stages/vanguard_factor_engine.py`
- `vanguard/helpers/db.py`
- `vanguard/helpers/clock.py`
- `vanguard/helpers/bars.py`
- `vanguard/data_adapters/alpaca_adapter.py`
- `vanguard/data_adapters/mt5_data_adapter.py`
- `vanguard/factors/price_location.py`
- `vanguard/factors/momentum.py`

3. A non-canonical backfill side path:
- `stages/vanguard_training_backfill_fast.py`

4. Real data/state already exists in the DB:
- `vanguard_bars_5m`
- `vanguard_bars_1h`
- `vanguard_cache_meta`
- `vanguard_health`
- `vanguard_features`
- `vanguard_execution_log`

5. Tests for the implemented subset are strong:
- `python3 -m pytest -q` passed: `189 passed in 0.88s`

### Partial / stubbed

These are present but only partial relative to the docs:

1. Stage 1 cache is partial:
- live implementation is Alpaca equity-centric
- MT5 adapter exists but is explicitly stub mode on non-Windows
- IBKR adapter file exists but is zero bytes
- data model stores `5m` and `1h`, not the doc-stated `1m canonical -> 5m derived -> 1h derived`

2. Stage 2 prefilter is partial:
- works for symbols in `vanguard_bars_5m`
- health logic is equity-market-hours-centric
- no clean multi-asset session model

3. Stage 3 factor engine is partial:
- only 2 factor modules are active: `price_location`, `momentum`
- only 10 features are actually computed
- output is `features_json` in `vanguard_features`, not a canonical `vanguard_factor_matrix` contract

4. Training/backfill is partial and off-spec:
- official `stages/vanguard_training_backfill.py` is zero bytes
- non-spec `stages/vanguard_training_backfill_fast.py` exists and creates `vanguard_training_data`
- it computes approximate/vectorized factors internally instead of reusing Stage 3 modules
- it writes `forward_return`, not the spec’s `label_long` / `label_short` TBM labels

5. Phase 0 executor is partial:
- `scripts/execute_daily_picks.py` is real and usable
- `scripts/eod_flatten.py` is zero bytes
- `vanguard/execution/mt5_adapter.py` is zero bytes
- config coverage is incomplete

### Missing

Completely missing in code today:

1. Core stage files:
- `stages/vanguard_model_trainer.py`
- `stages/vanguard_orchestrator.py`
- `stages/vanguard_risk_filters.py`
- `stages/vanguard_selection.py`
- `stages/vanguard_training_backfill.py`

2. Strategy/helper/model modules promised by docs but empty:
- almost all of `vanguard/strategies/*.py`
- most of `vanguard/helpers/*.py`
- most of `vanguard/factors/*.py`
- `vanguard/models/feature_contract.py`

3. Missing config files referenced by docs:
- `config/vanguard_strategies.json`
- `config/vanguard_orchestrator_config.json`
- `config/vanguard_instrument_specs.json`
- `config/vanguard_expected_bars.json`

4. Missing model artifacts:
- `models/` is effectively empty

5. Missing DB tables required by later stages/specs:
- `vanguard_shortlist`
- `vanguard_tradeable_portfolio`
- `vanguard_portfolio_state`
- `vanguard_session_log`
- `vanguard_model_registry`
- `vanguard_walkforward_results`

## 4. Stage-by-stage audit

### Stage 1

Intended from spec:
- multi-source data pipeline
- 1m canonical storage
- 5m and 1h derivation
- multi-asset session awareness
- live health monitoring and adapter support for Alpaca, IBKR, MT5

Actually built:
- `stages/vanguard_cache.py` is real and runnable
- Alpaca adapter is real and fetches US equity daily bars / 5m bars
- MT5 adapter exists, but is stubbed on non-Windows and mainly useful for backfill-style calls
- bars are normalized and written to:
  - `vanguard_bars_5m`
  - `vanguard_bars_1h`
- no `1m` canonical table exists

Reality gap:
- Stage 1 is equity-first, not multi-asset-first
- there is no IBKR implementation
- there is no canonical 1m storage layer
- V1 spec mentions `vanguard_prefilter_results`; actual code does not create that table
- config is hardcoded in stage constants, not config-driven

What blocks full V1 spec compliance:
- missing IBKR adapter
- MT5 still stubbed for this environment
- missing 1m table/schema
- missing config files for instrument/session contracts

### Stage 2

Intended from spec:
- thin live health monitor on V1 survivors
- 5 health checks
- session-aware across asset classes
- output contract `vanguard_health_results`

Actually built:
- `stages/vanguard_prefilter.py` is real and runnable
- it reads `vanguard_bars_5m`
- applies 5 checks:
  - staleness
  - relative volume
  - spread proxy
  - warm-up
  - session check
- writes to `vanguard_health`

Reality gap:
- output table name differs from spec wording (`vanguard_health` vs `vanguard_health_results`)
- logic assumes equity market hours via `helpers/clock.py`
- no per-asset-class session handling
- no explicit lane separation for 24/5 forex vs 24/7 crypto vs US equities

What blocks clean V2 as designed:
- no session abstraction module is implemented
- no config-driven thresholds
- no clean multi-asset input contract from V1

### Stage 3

Intended from spec:
- 35 features across 10 groups
- asset-class benchmarks
- SMC 5m and 1h modules
- factor-matrix table contract

Actually built:
- `stages/vanguard_factor_engine.py` is real and runnable
- registered modules:
  - `vanguard.factors.price_location`
  - `vanguard.factors.momentum`
- writes JSON payloads to `vanguard_features`
- DB currently contains `223` feature rows

Reality gap:
- only 10 features exist today
- all other factor modules are zero-byte files
- no SMC implementation
- no daily bridge implementation
- no market-context implementation
- no canonical factor matrix table
- factor output is JSON blob, not explicit columns

What blocks Stage 3 from feeding ML cleanly:
- missing majority of feature modules
- missing feature contract file
- no stable columnar factor schema
- SPY benchmark handling is equity-only

### Stage 4

Intended from specs:
- V4A training backfill with TBM labels `label_long` and `label_short`
- V4B model trainer with LightGBM + TCN, registry tables, walk-forward results

Actually built:
- official `vanguard_training_backfill.py`: zero bytes
- official `vanguard_model_trainer.py`: zero bytes
- non-canonical `vanguard_training_backfill_fast.py`: implemented

What `vanguard_training_backfill_fast.py` actually does:
- loads `vanguard_bars_5m`
- vectorizes approximate factor generation per ticker
- creates `vanguard_training_data`
- stores factor columns plus `forward_return`
- uses placeholder values for several session / HTF / cross-asset fields

Reality gap:
- no official Stage 4A implementation
- no official Stage 4B implementation
- fast backfill is useful engineering work, but it is off-contract
- no TBM long/short labels
- no model training artifacts
- no model registry / walk-forward results tables

What blocks Stage 4 entirely:
- no canonical labeling contract implemented
- no trainer
- no model outputs
- no registry tables

### Stage 5

Intended from spec:
- strategy router with multiple strategy modules
- ML gate
- shortlist output `vanguard_shortlist`

Actually built:
- `stages/vanguard_selection.py`: zero bytes
- all strategy modules under `vanguard/strategies/` are zero bytes

Reality gap:
- Stage 5 is missing, not partial
- no shortlist table exists
- no regime gate, no consensus counter, no strategy router helper

Blocking impact:
- there is no way to select intraday Vanguard picks from V3/V4 outputs

### Stage 6

Intended from spec:
- account-aware risk filters
- position sizing
- `vanguard_tradeable_portfolio`
- `vanguard_portfolio_state`

Actually built:
- `stages/vanguard_risk_filters.py`: zero bytes
- helper files that should support it are zero bytes:
  - `helpers/portfolio_state.py`
  - `helpers/position_sizer.py`
  - `helpers/correlation_checker.py`
  - `helpers/ttp_rules.py`

Reality gap:
- Stage 6 is missing, not partial
- no tradeable portfolio tables exist
- no risk engine exists

Blocking impact:
- even if Stage 5 existed, there is no Vanguard-native risk sizing/account routing layer

### Stage 7

Intended from spec:
- orchestrator loop
- session lifecycle
- execution bridge integration
- session log

Actually built:
- `stages/vanguard_orchestrator.py`: zero bytes
- Phase 0 executor bridge exists separately
- `scripts/execute_daily_picks.py` works as an executor for Meridian/S1 daily signals

Reality gap:
- V7 proper does not exist
- repo currently has an executor bridge, not a Vanguard orchestrator
- no `vanguard_session_log` table exists

Important nuance:
- Phase 0 executor is real
- V7 orchestrator is missing
- the repo therefore supports a daily-pick execution bridge, not a full Vanguard intraday runtime

## 5. Cross-cutting issues

### Repo structure

Strengths:
- staged layout is clear
- executor code is isolated under `vanguard/execution`
- helpers for DB/clock/bars are reusable and reasonably clean

Problems:
- docs say `AGENTS.md` / `ROADMAP.md`, actual files are `VANGUARD_AGENTS.md` / `VANGUARD_ROADMAP.md`
- many zero-byte files create a false sense of completeness
- `vanguard_training_backfill_fast.py` is a real implementation that sits outside the documented canonical stage path
- future build agents will see dozens of empty files and may assume they are partial implementations rather than placeholders

Practical consequence:
- repo structure is understandable for humans, but misleading for autonomous build agents unless the empty-file reality is made explicit

### Config / env

Docs/specs expect:
- multiple config JSON files
- config-driven runtime

Actual:
- only these configs exist:
  - `config/vanguard_accounts.json`
  - `config/vanguard_execution_config.json`
- most stage/runtime parameters are hardcoded constants inside stage files
- Alpaca and SignalStack/Telegram env vars are used directly

Result:
- Phase 0 executor config exists
- full staged config contract does not

### DB / schema contracts

Actual live DB contains:
- `vanguard_bars_5m`
- `vanguard_bars_1h`
- `vanguard_cache_meta`
- `vanguard_health`
- `vanguard_features`
- `vanguard_execution_log`

Actual live DB does not contain the later-stage tables promised by docs.

Concrete schema issues:
1. `vanguard_features` stores `features_json`, not a canonical factor matrix.
2. `vanguard_training_data` is not present in the inspected live DB, despite `vanguard_training_backfill_fast.py` being able to create it.
3. `vanguard_execution_log` schema in the live DB is older than current bridge expectations:
   - bridge code expects `signal_tier` and `signal_metadata`
   - live DB table currently does not have those columns
   - this is a real schema drift between code and DB state

### Orchestration / entrypoints

Real runnable entrypoints today:
- `stages/vanguard_cache.py`
- `stages/vanguard_prefilter.py`
- `stages/vanguard_factor_engine.py`
- `stages/vanguard_training_backfill_fast.py`
- `scripts/execute_daily_picks.py`

Not runnable because empty:
- `stages/vanguard_training_backfill.py`
- `stages/vanguard_model_trainer.py`
- `stages/vanguard_selection.py`
- `stages/vanguard_risk_filters.py`
- `stages/vanguard_orchestrator.py`
- `scripts/eod_flatten.py`

End-to-end consequence:
- a full V1→V7 run is impossible today
- the only end-to-end runtime that exists is:
  - external source systems (Meridian/S1) -> `execute_daily_picks.py` -> execution bridge -> `vanguard_execution_log`

### Testing / validation

Good news:
- current implemented subset has real tests
- `python3 -m pytest -q` passed: `189 passed`

Coverage reality:
- tests cover:
  - SignalStack adapter
  - Telegram alerts
  - tier loading in `execute_daily_picks.py`
  - V1 helper logic
  - V2 health logic
  - V3 partial factor engine

Missing coverage:
- zero-byte tests for:
  - MT5 execution adapter
  - orchestrator
  - risk
  - selection

So the green test suite means:
- built pieces are decent
- not that Vanguard as a whole is built

### Spec mismatches

Key mismatches between docs and code:

1. Multi-asset vs equity-first:
- docs: multi-asset from day 1
- code: V1/V2/V3 are overwhelmingly US-equity centric

2. 1m canonical bars:
- docs: 1m canonical, 5m/1h derived
- code: only `vanguard_bars_5m` and `vanguard_bars_1h`

3. Stage 3 feature contract:
- docs/spec: 35-feature factor matrix
- code: 10 features in JSON blobs

4. Stage 4 official implementation:
- docs: V4A/V4B approved and canonical
- code: official files empty; unofficial fast backfill exists

5. Config-driven claim:
- docs: config-driven, no hardcoded paths/times/rules
- code: multiple hardcoded thresholds/constants in stage files

6. Daily vs intraday lane mixing:
- docs describe Vanguard as intraday multi-asset
- current meaningful runtime is mostly a daily pick executor for Meridian/S1

## 6. Severity summary

### Blockers

1. No full pipeline exists beyond V3.
- V4A official file empty
- V4B empty
- V5 empty
- V6 empty
- V7 empty

2. No canonical orchestrator/runtime path.
- cannot run full Vanguard end to end

3. DB/schema contract is incomplete and drifting.
- later-stage tables missing
- live `vanguard_execution_log` is behind current bridge code

4. Config contract is incomplete.
- several required config files from docs do not exist

5. Daily/intraday lane separation is not clean.
- repo currently mixes:
  - daily executor bridge for Meridian/S1
  - partial intraday equity sandbox for Vanguard

### Should fix soon

1. Decide canonical ownership of Stage 4A.
- either promote `vanguard_training_backfill_fast.py` into the official stage
- or replace it
- current duality is confusing

2. Make file naming and doc naming consistent.
- `VANGUARD_AGENTS.md` vs docs that say `AGENTS.md`
- same for roadmap

3. Add missing config scaffolding now, before more code lands.
- otherwise future stages will hardcode more behavior

4. Reconcile DB schema bootstrap with live DB state.
- especially `vanguard_execution_log`

5. Make the repo explicit about empty files.
- otherwise future build agents may treat zero-byte modules as half-implemented

### Ignore for now

1. Empty `models/` directory by itself.
- this is expected until V4B exists

2. Empty MT5 execution adapter.
- acceptable if Phase 0 remains TTP/SignalStack only

3. Futures lane details.
- no need to flesh out until Phase 5 if Phase 0-4 are still incomplete

## 7. Recommended next build order

1. Finish Phase 0 properly before adding more speculative intraday code.
- implement `scripts/eod_flatten.py`
- reconcile `vanguard_execution_log` schema migration with the live DB
- document the exact Meridian/S1 source contracts used by `execute_daily_picks.py`
- keep executor bridge clean and self-contained

2. Lock the canonical repo contract.
- create the missing config files, even if minimal
- decide whether file names stay `VANGUARD_*` or become `AGENTS.md` / `ROADMAP.md`
- explicitly mark zero-byte files as TODO scaffolds or remove them until implemented

3. Finish Stage 1-3 canonically before touching Stage 5-7.
- V1: decide whether Vanguard truly uses `1m` canonical or not
- V1/V2: implement session logic for non-equity asset classes or narrow the scope explicitly
- V3: finish the factor contract and decide whether output is JSON blob or real factor matrix

4. Resolve Stage 4A officially.
- the safest next build step is to either:
  - adopt `vanguard_training_backfill_fast.py` as the canonical V4A basis and bring it onto spec, or
  - delete/replace it and implement the official V4A file
- do not keep both “official empty” and “fast unofficial” paths for long

5. Build V4B next, not V5.
- without real trained models, Stage 5 router behavior will be speculative

6. Then implement V5, V6, and V7 in order.
- selection
- risk
- orchestrator

Bottom line:
- Vanguard today is best described as:
  - a usable Phase 0 daily execution bridge for Meridian/S1
  - plus a partial equity-only intraday data/factor sandbox
  - not a coherent multi-asset V1-V7 trading system yet
