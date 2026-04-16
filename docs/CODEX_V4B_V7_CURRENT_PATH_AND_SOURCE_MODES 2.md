# Vanguard QA Current Path: V4B to V7, Source Modes, and What Stays

## Purpose

This document captures the **current QA downstream path** after the V5/V6/Risky cutover work and the crypto validation investigation.

It does two things:

1. Documents the actual **V4B -> V7** path now running in QA, with the key files.
2. Separates **permanent architecture fixes** from **temporary validation-only branches** so we do not accidentally revert the good changes or ship the wrong ones.

This is written for the current MT5/GFT baseline, with the explicit note that a future non-MT5 prop firm path will need different context-layer ownership.

---

## FTMO Onboarding State

FTMO now exists in runtime/risk/config as a first-class MT5-backed provider:

- profile: [vanguard_runtime.json](/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json) `ftmo_demo_100k`
- context source: [vanguard_runtime.json](/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json) `ftmo_mt5_local`
- universe: [vanguard_runtime.json](/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json) `ftmo_universe`
- risk profile: [risk_rules.json](/Users/sjani008/SS/Risky/config/risk_rules.json) `ftmo_demo_100k`

Current nuance:

- FTMO is intentionally **path-pending** until the second macOS user’s MT5 Files path is filled.
- Runtime/context/risk can now represent GFT and FTMO separately.
- V1 market-data polling is still effectively **single active MT5 source per asset class** in the current orchestrator.
- So the FTMO onboarding is structurally complete, but **simultaneous dual-terminal MT5 polling in one loop** still needs a later multi-adapter V1 refactor if we want both GFT and FTMO broker bars live at the same time.

---

## Active Runtime Baseline

Current active baseline in [vanguard_runtime.json](/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json):

- **Crypto primary source**: `mt5_local`
- **Crypto baseline universe**:
  - `BCHUSD`
  - `BNBUSD`
  - `BTCUSD`
  - `ETHUSD`
  - `LTCUSD`
  - `SOLUSD`
- **Alternative source kept but inactive**: `twelvedata`
- **Crypto validation threshold relaxer**: present but **disabled**

Why:

- For MT5-backed providers like GFT/FTMO, the stable baseline is:
  - V1 market data aligned with broker truth
  - context daemon aligned with broker truth
  - V5.1/V6 using the same source family
- Twelve Data remains in config as a **future alternative provider path**, not the active baseline.

---

## Current V4B -> V7 Path

### V1 Market Data

Purpose:
- poll the active market-data source for the active asset class
- write canonical bar tables

Key files:
- [stages/vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_orchestrator.py)
- [vanguard/data_adapters/twelvedata_adapter.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/data_adapters/twelvedata_adapter.py)
- [vanguard/data_adapters/mt5_dwx_adapter.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/data_adapters/mt5_dwx_adapter.py)
- [vanguard/helpers/db.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/helpers/db.py)

Current truth:
- source routing must come from runtime config
- no hardcoded `FTMO -> TwelveData` assumptions remain in the active path
- V1 now respects runtime-config market-data source selection

Important nuance:
- the bar tables are shared physical tables
- the active source for a symbol must therefore be enforced at read time, not assumed from “latest timestamp wins”

---

### V2 Prefilter

Purpose:
- health-check symbols before factor computation
- write `vanguard_health`

Key file:
- [stages/vanguard_prefilter.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_prefilter.py)

Current truth:
- V2 thresholds are now runtime-driven and asset-specific
- V2 reads bars filtered by the symbol’s configured `data_source`
- V2 no longer silently mixes stale bars from the wrong source just because they have a later timestamp

Important nuance:
- `vanguard_health.data_source` matters
- V3 must inherit the same source choice; otherwise V2 and V3 drift

---

### V3 Factor Engine

Purpose:
- compute factor rows from V2 survivors
- write `vanguard_features` and `vanguard_factor_matrix`

Key file:
- [stages/vanguard_factor_engine.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_factor_engine.py)

Current truth:
- V3 now loads bars filtered by the survivor row’s `data_source`
- V3 no longer reads “latest bars from anywhere” for active-source survivors

Important nuance:
- benchmark bars are also loaded with the same asset-class source context where possible
- this matters for crypto validation and future alternate-provider paths

---

### V4B Scoring

Purpose:
- score the current factor matrix with the active model
- write `vanguard_predictions`

Key files:
- [stages/vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_orchestrator.py)
- [stages/vanguard_factor_engine.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_factor_engine.py)

Current truth:
- BCH feature-contract mismatch is fixed
- crypto missing VP rows no longer zero out shared/base `volume_delta`
- forex VP override behavior remains intact

Important nuance:
- this fix is permanent; it is not tied to crypto validation mode

---

### Context Layer / Pair-State

Purpose:
- write live measurements truth
- do not make decisions

Key files:
- [scripts/dwx_live_context_daemon.py](/Users/sjani008/SS/Vanguard_QAenv/scripts/dwx_live_context_daemon.py)
- [vanguard/selection/crypto_symbol_state.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/selection/crypto_symbol_state.py)
- [vanguard/context_health.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/context_health.py)

Current truth:
- broker/DWX context writes into:
  - `vanguard_context_quote_latest`
  - `vanguard_context_quote_history`
  - `vanguard_context_symbol_specs`
  - `vanguard_context_account_latest`
  - `vanguard_context_positions_latest`
  - `vanguard_context_orders_latest`
- crypto symbol-state now honors the runtime primary source instead of blindly reusing the first quote row it finds

Important nuance:
- `vanguard_crypto_symbol_state` is still allowed to use bar fallback in validation mode
- that fallback must respect the active runtime source
- for MT5 baseline, this should normally stay broker-aligned

---

### V5.1 Tradeability

Purpose:
- economics only
- write `vanguard_v5_tradeability`

Current truth:
- V5.1 consumes context/pair-state truth
- it does not own routing, risk, or execution
- `PASS | WEAK | FAIL` remains the intended contract

Important nuance:
- for FX/crypto sizing, Risky now joins back to V5.1 by:
  - `cycle_ts_utc`
  - `profile_id`
  - `symbol`
  - `direction`

---

### V5.2 Selection

Purpose:
- model-first selection after economics gating
- write `vanguard_v5_selection`
- mirror selected rows to legacy shortlist tables for V6 compatibility

Key file:
- [vanguard/selection/shortlisting.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/selection/shortlisting.py)

Current truth:
- selection is model-first
- route tiers are annotations only
- legacy shortlist mirrors remain active to reduce blast radius

---

### Risky / Sizing

Purpose:
- canonical policy truth
- final sizing owner for FX and crypto

Key files:
- [Risky/config/risk_rules.json](/Users/sjani008/SS/Risky/config/risk_rules.json)
- [vanguard/risk/risky_policy_adapter.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/risky_policy_adapter.py)
- [vanguard/risk/policy_engine.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/policy_engine.py)
- [vanguard/risk/risky_sizing.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/risky_sizing.py)
- [stages/vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_risk_filters.py)

Current truth:
- runtime is now the binding layer
- `risk_rules.json` is the canonical policy/risk truth
- FX/crypto sizing uses DB truth and joins back to:
  - `vanguard_v5_tradeability`
  - `vanguard_v5_selection`
- `vanguard_risky_decisions` is the canonical audit table

Important nuance:
- account precedence is explicit:
  1. `vanguard_context_account_latest`
  2. `vanguard_account_state` bootstrap fallback
- no synthetic live spread substitution is allowed in active FX/crypto sizing

---

### V6 Health

Purpose:
- operational readiness only
- freshness / context / routeability

Key file:
- [vanguard/context_health.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/context_health.py)

Current truth:
- V6 health runs after Risky approval
- it writes `vanguard_v6_context_health`
- it can downgrade or block routeability without redoing policy/risk logic

Important nuance:
- crypto validation mode still exists as a separate branch
- that branch should be treated as **validation-only**, not the default MT5 baseline

---

### V7 Orchestrator

Purpose:
- run the staged pipeline
- read `vanguard_tradeable_portfolio`
- hand off approved rows to the executor bridge

Key file:
- [stages/vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_orchestrator.py)

Current truth:
- V7 contract is intentionally unchanged for now
- V6 still outputs `vanguard_tradeable_portfolio`
- execution/lifecycle cleanup is still pending

---

## Permanent Fixes vs Validation-Only Branches

### Permanent Fixes: Keep

These should stay.

1. **Runtime-driven source routing**
- no hardcoded FTMO/TwelveData shortcuts
- active source selected from runtime config

2. **Source-consistent V2/V3 reads**
- V2 and V3 filter bars by the configured active source
- this prevents silent mixed-source inference

3. **BCH / base `volume_delta` fix**
- crypto with missing VP rows preserves shared/base `volume_delta`

4. **DB-truth Risky sizing**
- FX/crypto sizing uses context/pair-state/account DB truth
- no raw DWX-file fallback in active sizing

5. **Risk JSON as canonical policy truth**
- runtime binds profile to `risk_profile_id`
- `risk_rules.json` owns live rule bodies

6. **`vanguard_risky_decisions` audit trail**
- required for post-trade and post-reject analysis

7. **Crypto symbol-state source selection honoring runtime**
- even when fallback exists, it must follow the configured source branch

---

### Validation-Only Branches: Keep Inactive By Default

These may still be useful later, but should remain inactive in the MT5 baseline.

1. **`v2_prefilter.crypto_validation`**
- purpose: allow a non-MT5 weekend crypto validation run to reach V5/V6
- status: now **disabled**

2. **Twelve Data as primary crypto source**
- purpose: future provider path where there is no MT5 broker truth
- status: present in config, but **not active baseline**

3. **Crypto bar-fallback path in `vanguard_crypto_symbol_state`**
- purpose: validation mode only
- status: code remains because a non-MT5 branch may need it
- baseline should avoid relying on it when broker truth exists

4. **Crypto validation mode in V6 health**
- purpose: prove downstream mechanics without live routing
- status: still available, but not the baseline approval model for MT5-backed providers

---

## What Must Be Different for a Non-MT5 Provider

If a future prop firm does **not** use MT5, the path cannot simply toggle `primary_source_by_asset_class.crypto = twelvedata` and call it done.

The following must be rethought:

1. **Context layer ownership**
- if there is no broker context, `vanguard_context_quote_latest` is no longer broker truth
- quote truth may come from the market-data source itself
- account/position/order truth may come from another execution venue or provider API

2. **V5.1 economics**
- if spread is not broker-executable spread, economics must reflect what the execution venue can actually fill
- a bar-range proxy is not enough for live economics

3. **V6 health**
- quote freshness rules must follow the active source branch
- account truth requirements must match the execution venue actually used

4. **Executor and lifecycle**
- DWX assumptions disappear
- entry/modify/close and account/position feedback must come from the new venue

In short:
- **MT5 baseline**: one coherent broker-aligned path
- **non-MT5 provider**: separate branch, not just a market-data toggle

---

## Immediate Next Step

Do **not** keep extending the old weekend crypto hack just to manufacture a selected pick.

The correct next work item is:

- executor / lifecycle / autoclose cleanup on the **real MT5/DWX baseline path**

That work should assume:
- runtime crypto baseline = DWX
- forex baseline = DWX context + current broker path
- Twelve Data remains an alternative future branch, not the active baseline

---

## Related Files

Core runtime/source config:
- [vanguard_runtime.json](/Users/sjani008/SS/Vanguard_QAenv/config/vanguard_runtime.json)

Stages:
- [stages/vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_orchestrator.py)
- [stages/vanguard_prefilter.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_prefilter.py)
- [stages/vanguard_factor_engine.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_factor_engine.py)
- [stages/vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard_QAenv/stages/vanguard_risk_filters.py)

Context / symbol state:
- [scripts/dwx_live_context_daemon.py](/Users/sjani008/SS/Vanguard_QAenv/scripts/dwx_live_context_daemon.py)
- [vanguard/context_health.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/context_health.py)
- [vanguard/selection/crypto_symbol_state.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/selection/crypto_symbol_state.py)

Risk / sizing:
- [Risky/config/risk_rules.json](/Users/sjani008/SS/Risky/config/risk_rules.json)
- [vanguard/risk/risky_policy_adapter.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/risky_policy_adapter.py)
- [vanguard/risk/policy_engine.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/policy_engine.py)
- [vanguard/risk/risky_sizing.py](/Users/sjani008/SS/Vanguard_QAenv/vanguard/risk/risky_sizing.py)

Existing supporting notes:
- [QA_RISKY_SIZING_CUTOVER.md](/Users/sjani008/SS/Vanguard_QAenv/docs/QA_RISKY_SIZING_CUTOVER.md)
- [CODEX_VANGUARD_V4B_V5_REWRITE.md](/Users/sjani008/SS/Vanguard_QAenv/docs/CODEX_VANGUARD_V4B_V5_REWRITE.md)
- [VANGUARD_ORCHESTRATOR_ARCHITECTURE_MAP.md](/Users/sjani008/SS/Vanguard_QAenv/docs/VANGUARD_ORCHESTRATOR_ARCHITECTURE_MAP.md)
