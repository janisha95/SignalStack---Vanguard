**EXECUTIVE SUMMARY**
- The live operator system is **not** the staged Vanguard V1→V6 pipeline. The live path is the merged Meridian UI → [unified_api.py:247](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:247) → adapters + [trade_desk.py:605](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:605) → SignalStack.
- Vanguard V1–V4 are materially built and have real data in `/Users/sjani008/SS/Vanguard/data/vanguard_universe.db`: `vanguard_bars_5m=1,188,544`, `vanguard_bars_1h=113,317`, `vanguard_health=257`, `vanguard_features=239`, `vanguard_training_data=38,132`, `vanguard_model_registry=2`, `vanguard_walkforward_results=2`.
- Vanguard V5/V6 code exists, but their runtime outputs do not: `vanguard_shortlist` and `vanguard_tradeable_portfolio` are both missing from the live DB even though [vanguard_selection.py:75](/Users/sjani008/SS/Vanguard/stages/vanguard_selection.py:75) and [vanguard_risk_filters.py:96](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py:96) create/write them.
- Vanguard V7 orchestration is still missing. [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py) is empty.
- The merged UI is real and usable for S1 + Meridian. Vanguard is exposed in the contract but not actually available because [vanguard_adapter.py:17](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py:17) still returns `[]`.
- Test coverage exists but is currently regressed: the targeted Vanguard/unified suite finished with **126 passed, 23 failed**, concentrated in [test_unified_api.py](/Users/sjani008/SS/Vanguard/tests/test_unified_api.py). The failures are mostly contract drift, not absence of code.

**SYSTEM MAP**
- Current lanes/services:
  - S1 daily lane: [s1_adapter.py:144](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:144) reads `scorer_predictions` plus latest convergence JSON.
  - Meridian daily lane: [meridian_adapter.py:94](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:94) reads `shortlist_daily`, joins `predictions_daily`, falls back to `daily_bars`.
  - Vanguard staged intraday lane: [vanguard_cache.py:407](/Users/sjani008/SS/Vanguard/stages/vanguard_cache.py:407) → [vanguard_prefilter.py:218](/Users/sjani008/SS/Vanguard/stages/vanguard_prefilter.py:218) → [vanguard_factor_engine.py:175](/Users/sjani008/SS/Vanguard/stages/vanguard_factor_engine.py:175) → [vanguard_model_trainer.py](/Users/sjani008/SS/Vanguard/stages/vanguard_model_trainer.py) → [vanguard_selection.py:180](/Users/sjani008/SS/Vanguard/stages/vanguard_selection.py:180) → [vanguard_risk_filters.py:580](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py:580).
- Candidate generation path:
  - Live UI candidates come from [unified_api.py:247](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:247), which merges Meridian + S1 + placeholder Vanguard.
  - Vanguard staged candidate generation stops before UI because [vanguard_adapter.py:22](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py:22) is still a placeholder.
- Strategy routing path:
  - Real staged router: [strategy_router.py:58](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py:58), [strategy_router.py:165](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py:165), [strategy_router.py:195](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py:195).
  - It imports 13 strategy classes at [strategy_router.py:60](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py:60)-[72](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py:72).
- Risk filter path:
  - Staged V6 engine: [vanguard_risk_filters.py:157](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py:157), [vanguard_risk_filters.py:245](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py:245), [vanguard_risk_filters.py:580](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py:580).
  - Live pre-execution gate: [trade_desk.py:465](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:465).
- Pre-execution gate path:
  - `/trades` UI in [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx) calls preview/execute via [unified-api.ts:193](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:193) → [unified_api.py:433](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:433) → [trade_desk.py:605](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:605).
- Execution path:
  - Live merged path writes `execution_log` via [trade_desk.py:283](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:283) and submits through [signalstack_adapter.py:90](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py:90).
  - Parallel staged executor path writes `vanguard_execution_log` via [bridge.py:63](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py:63) and [bridge.py:192](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py:192).
- Logging/journal/state path:
  - Live UI reads `execution_log` through [unified_api.py:450](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:450), [unified_api.py:831](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:831).
  - Staged executor writes a different table, `vanguard_execution_log`.
- UI data flow path:
  - Nav is daily-shell-centric in [app-shell.tsx:10](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/app-shell.tsx:10)-[14](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/app-shell.tsx:14).
  - `/candidates` mounts [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx) via [app/candidates/page.tsx:1](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/candidates/page.tsx:1).
  - `/trades` mounts [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx) via [app/trades/page.tsx:1](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/trades/page.tsx:1).

**IMPLEMENTATION TRUTH TABLE**

| Subsystem | Status | Truth |
|---|---|---|
| Candidate sourcing | `partial` | S1 and Meridian are live via adapters; Vanguard source is empty because [vanguard_adapter.py:27](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py:27) returns `[]`. |
| Strategy router | `implemented` | Real V5 router exists in [strategy_router.py](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py) and [vanguard_selection.py](/Users/sjani008/SS/Vanguard/stages/vanguard_selection.py). |
| Scoring / ranking | `partial` | LGBM training artifacts exist in `vanguard_model_registry`, and `predict_probabilities()` is real, but no live Vanguard shortlist is materialized. |
| Risk filters | `partial` | Full staged V6 file exists, but live execution uses a separate API-local gate instead of [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py). |
| Pre-execution gate | `implemented` | Live gate exists in [trade_desk.py:465](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:465) and is wired into `/api/v1/execute`. |
| Execution / order handoff | `partial` | SignalStack handoff is live; TTP mapping is fixed in [signalstack_adapter.py:39](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py:39). But there are two executor paths and two log tables. |
| Journaling / logging | `partial` | `execution_log` and `vanguard_execution_log` coexist. Current DB snapshot: `execution_log=0`, `vanguard_execution_log=25`. |
| UI integration | `partial` | Merged UI is real for S1 + Meridian. Vanguard is selectable but not populated. |
| Source-aware contracts | `partial` | Row schema is source-aware, but `combined` still concatenates heterogeneous rows and exposes source-specific fields in one workspace. |
| Saved views / userviews | `implemented` | CRUD is real in [userviews.py](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py); only 3 canonical system views are seeded at [userviews.py:40](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:40). |
| Trade history / state surfaces | `partial` | Trade log, analytics, fill updates, close endpoint, and portfolio live endpoint exist, but portfolio state for Vanguard staged accounts is still `0` rows. |
| Config / env handling | `partial` | Real JSON configs and account profiles exist; webhook fallback-to-forward-track behavior is in [trade_desk.py:623](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:623). |
| Error / null handling | `partial` | Adapters have fallbacks, but tests show brittle assumptions such as Meridian requiring `predictions_daily`. |
| Tests / validation coverage | `partial` | Test suite exists and is meaningful, but current unified API suite is failing on 23 cases. |
| Dead / unused paths | `present` | [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py) is empty, [mt5_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/execution/mt5_adapter.py) is empty, [llm_strategy.py:95](/Users/sjani008/SS/Vanguard/vanguard/strategies/llm_strategy.py:95) logs “not implemented”. |

**SPEC VS CODE GAPS**
- V1 / cache:
  - Spec says built; code agrees. [vanguard_cache.py](/Users/sjani008/SS/Vanguard/stages/vanguard_cache.py) is real and DB bars exist.
- V2 / prefilter:
  - Spec says built; code agrees. [vanguard_prefilter.py](/Users/sjani008/SS/Vanguard/stages/vanguard_prefilter.py) writes `vanguard_health`, and DB has 257 rows.
- V3 / factor engine:
  - Spec says built; code agrees. [vanguard_factor_engine.py](/Users/sjani008/SS/Vanguard/stages/vanguard_factor_engine.py) writes `vanguard_features`, and DB has 239 rows.
- V4A / training backfill:
  - Spec says built; code agrees. [vanguard_training_backfill.py](/Users/sjani008/SS/Vanguard/stages/vanguard_training_backfill.py) is real; DB has `vanguard_training_data=38,132`.
- V4B / model training:
  - Spec says built; code mostly agrees. [vanguard_model_trainer.py](/Users/sjani008/SS/Vanguard/stages/vanguard_model_trainer.py) writes model registry and walk-forward results; DB has 2 models and 2 walk-forward rows.
- V5 / strategy router:
  - Docs in [Vanguard_CURRENT_STATE.md:25](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md:25) and [Vanguard_CURRENT_STATE.md:60](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md:60) say “NOT BUILT”.
  - Code contradicts that: [strategy_router.py](/Users/sjani008/SS/Vanguard/vanguard/helpers/strategy_router.py), concrete strategies, and [vanguard_selection.py](/Users/sjani008/SS/Vanguard/stages/vanguard_selection.py) are real.
  - Runtime gap: no `vanguard_shortlist` table currently exists.
- V6 / risk:
  - Docs in [Vanguard_CURRENT_STATE.md:26](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md:26) and [Vanguard_CURRENT_STATE.md:75](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md:75) say “NOT BUILT”.
  - Code contradicts that: [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py) is substantial.
  - Runtime gap: no `vanguard_tradeable_portfolio` table exists, and the live execution path uses [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py) instead.
- V6 enhancement pre-execution gate:
  - Spec in [V6_ENHANCEMENT_PRE_EXECUTION_GATE.md](/Users/sjani008/SS/Vanguard/docs/V6_ENHANCEMENT_PRE_EXECUTION_GATE.md) says integrate into existing V6 risk filters.
  - Code only partially matches: the gate is implemented, but in [trade_desk.py:465](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:465), not in [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py).
- V7 / orchestrator:
  - Spec exists, code does not. [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py) is empty.
- V7.1 executor:
  - Spec says done; code exists in [execute_daily_picks.py](/Users/sjani008/SS/Vanguard/scripts/execute_daily_picks.py) and [bridge.py](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py).
  - Gap: it is a daily S1 + Meridian tier executor, not the intraday Vanguard V1→V6 executor.
- Merged UI contract:
  - [BACKEND_CONTRACT.md](/Users/sjani008/SS/Vanguard/docs/UI%20Docs/BACKEND_CONTRACT.md) assumes populated shared fields.
  - Code contradicts this for Vanguard because [field_registry.py:139](/Users/sjani008/SS/Vanguard/vanguard/api/field_registry.py:139) advertises Vanguard-native fields while [vanguard_adapter.py:27](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py:27) returns no rows.

**END-TO-END BLOCKERS**
- No live Vanguard candidate surface:
  - `vanguard_shortlist` missing, `vanguard_adapter` placeholder, UI `source=vanguard` empty.
- No orchestrator:
  - Nothing in repo currently drives V1→V6 intraday cycle end to end. [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py) is empty.
- Duplicate risk paths:
  - staged V6 in [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py)
  - live gate in [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py)
  - they are not the same system.
- Duplicate execution/logging truth:
  - [bridge.py](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py) writes `vanguard_execution_log`
  - [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py) writes `execution_log`
  - operator truth can diverge immediately.
- Live operator flow is daily-picks oriented, not Vanguard intraday:
  - [trade_desk.py:212](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:212) and [execute_daily_picks.py:210](/Users/sjani008/SS/Vanguard/scripts/execute_daily_picks.py:210) both assemble S1 + Meridian tier picks.
- Validation is regressed:
  - 23 current test failures mean the repo does not have a stable, trusted contract even where code exists.

**UI BLOCKERS**
- Source-aware truth is violated by premature Vanguard exposure:
  - the UI exposes `VANGUARD` source and Vanguard-native fields, but backend returns no Vanguard candidates.
- No fake cross-lane score is only partially respected:
  - there is no single `primary_score`, which is good
  - but `combined` mode still lets heterogeneous rows coexist in one table and be sorted/filtered on source-specific fields.
- Silent field-meaning drift exists:
  - `final_score` is Meridian-only
  - `scorer_prob` / `p_tp` / `nn_p_tp` are S1-only
  - Vanguard-native fields are defined but not populated
  - the workspace does not strongly prevent cross-source misuse.
- Backend merge assumptions are real:
  - S1 rows are DB + latest convergence JSON
  - Meridian rows are shortlist + predictions + daily bars
  - `combined` in [unified_api.py:260](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:260)-[265](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:265) just concatenates them.
- UI compliance/risk is duplicated:
  - [trades-client.tsx:316](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx:316) still builds client-side compliance while backend gate is authoritative. That duplication can drift.

**PRIORITIZED FIX PLAN**
- `P0 must-fix for truth/safety`
  - Pick one execution truth surface. Either keep `execution_log` + `trade_desk.py` as canonical or restore `bridge.py`/`vanguard_execution_log` as canonical. Do not keep both live.
  - Either implement [vanguard_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py) from `vanguard_shortlist` or hide/remove Vanguard from UI and field registry until it exists.
  - Materialize `vanguard_shortlist` and `vanguard_tradeable_portfolio` in the real DB or stop claiming V5/V6 availability.
  - Fix the failing unified API tests before adding new features.
- `P1 must-fix for end-to-end usability`
  - Wire one real intraday runner for V1→V6. That can be a minimal runner; it does not need a broad redesign, but it must exist.
  - Reconcile pre-execution gate vs V6 risk engine. Either call [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py) or explicitly decommission it.
  - Remove stale “V5 Strategy Router not built” messaging from [unified_api.py:123](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:123) and stale status claims from [Vanguard_CURRENT_STATE.md](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md).
- `P2 cleanup / architecture hardening`
  - Deduplicate tier assembly between [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py) and [execute_daily_picks.py](/Users/sjani008/SS/Vanguard/scripts/execute_daily_picks.py).
  - Remove or hide phantom Vanguard fields from [field_registry.py](/Users/sjani008/SS/Vanguard/vanguard/api/field_registry.py) until real rows exist.
  - Make contract tests source-aware and add fixture support for optional joined tables like `predictions_daily`.
- `P3 polish / deferred`
  - LLM strategy implementation
  - MT5/FTMO lane implementation
  - richer report surfaces and portfolio state automation

**RECOMMENDED BUILD ORDER**
1. Freeze the canonical runtime path: `unified_api` + `trade_desk` or staged `bridge`; not both.
2. Make Vanguard V5 produce a real `vanguard_shortlist` in the live DB.
3. Implement `vanguard_adapter.get_candidates()` from `vanguard_shortlist` and only then expose Vanguard rows in the merged UI.
4. Decide whether [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py) is the real V6 engine. If yes, call it from execution preview/submit. If no, retire it and rename the API-local gate as the canonical V6.
5. Make one logging table canonical and migrate the other path to it.
6. Restore test truth: fix [test_unified_api.py](/Users/sjani008/SS/Vanguard/tests/test_unified_api.py) breakages caused by the new joins/response shapes.
7. Only after 1–6, add orchestration around the proven path in [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py).

No files were modified for this audit.
**EXECUTIVE SUMMARY**
- I audited the live code surface, not the plans. The current live operator stack is the merged UI in [signalstack-app](/Users/sjani008/SS/Meridian/ui/signalstack-app) talking to [unified_api.py](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py), which then fans out to S1 and Meridian adapters plus shared trade-desk logic.
- The handoff/current-state docs are useful for intent, but not reliable for live contract truth. [PROJECT_VANGUARD_HANDOFF.md](/Users/sjani008/SS/Vanguard/docs/PROJECT_VANGUARD_HANDOFF.md) is mostly architectural intent. [Vanguard_CURRENT_STATE.md](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md) is materially stale on V5/V6. [MERIDIAN_CURRENT_STATE.md](/Users/sjani008/SS/Meridian/MERIDIAN_CURRENT_STATE.md) is accurate for Meridian’s own daily pipeline, not for the merged unified contracts.
- Current live candidate truth is:
  - S1: live
  - Meridian: live
  - Vanguard: placeholder in unified API
- Current live trade truth is:
  - UI path uses [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py) and `execution_log`
  - a separate staged execution path exists in [bridge.py](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py) and `vanguard_execution_log`
  - that is a duplicate truth surface
- Current live contract drift is concentrated in three areas:
  - Vanguard is exposed before it is real
  - combined mode overstates uniformity across lanes
  - frontend normalization drops or weakens some backend fields
- For the new v2 contracts, the safest scope is:
  - contractize S1 + Meridian + shared trade desk now
  - treat Vanguard candidate rows as out-of-scope or explicitly placeholder
  - treat bridge/orchestrator paths as staged, not current live truth

**API INVENTORY**

| method | route | owner file | purpose | lane/source | status | request shape summary | response shape summary | error shape summary |
|---|---|---|---|---|---|---|---|---|
| `GET` | `/api/v1/health` | [unified_api.py:69](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:69) | source availability + webhook/telegram config | shared | `live`, but stale for Vanguard | none | `{status, sources:{meridian,s1,vanguard}, webhook, telegram}` | 200 only in normal path; no structured failure handling |
| `GET` | `/api/v1/field-registry` | [unified_api.py:133](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:133) | field metadata for candidates UI | shared | `live`, partially misleading | none | `{fields:[...], count}` | generic 500 if import/runtime fails |
| `GET` | `/api/v1/candidates` | [unified_api.py:247](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:247) | candidate list by source | S1 / Meridian / Vanguard / combined | `live` for S1+Meridian, `placeholder` for Vanguard, `partial` for combined | query: `source`, `direction`, `sort`, `filters`, `page`, `page_size`, `date` | `{candidates, rows, total, count, source, as_of, updated_at, page, page_size, total_pages, field_registry}` | no explicit validation except query types; bad adapter internals degrade to empty rows |
| `GET` | `/api/v1/candidates/{row_id}` | [unified_api.py:296](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:296) | candidate detail by row id | lane-specific | `live` for S1+Meridian, `placeholder` for Vanguard | path `row_id` | returns full row dict from selected adapter | `400 {"detail": invalid row_id/unknown source}`, `404 {"detail": not found}` |
| `GET` | `/api/v1/userviews` | [unified_api.py:353](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:353) | list saved/system views | shared | `live` | none | `{views:[...], count}` | generic 500 |
| `POST` | `/api/v1/userviews` | [unified_api.py:359](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:359) | create saved view | shared | `live` | `UserviewCreate` | created view object | FastAPI validation errors; generic 500 |
| `PUT` | `/api/v1/userviews/{view_id}` | [unified_api.py:365](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:365) | update saved view | shared | `live` | partial `UserviewUpdate` | updated view object | `404 {"detail":"View not found..."}` |
| `DELETE` | `/api/v1/userviews/{view_id}` | [unified_api.py:374](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:374) | delete custom view | shared | `live` | path id | `{deleted:true,id}` | `403` for system views, `404` missing |
| `GET` | `/api/v1/picks/today` | [unified_api.py:425](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:425) -> [trade_desk.py:212](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:212) | 7 daily pick tiers | S1 + Meridian only | `live`, daily-only | query `date?` | `{date, tiers:[{tier,label,source,picks}], total_picks}` | generic 500 |
| `POST` | `/api/v1/execute` | [unified_api.py:432](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:432) -> [trade_desk.py:605](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:605) | trade preview + execution | shared trade desk | `live` | `ExecuteRequest { trades[], profile_id?, preview_only }` | `{approved[], rejected[], summary, submitted, failed, results[]}` | usually returns 200 with rejects/failures; hard failures bubble as 500 |
| `GET` | `/api/v1/execution/log` | [unified_api.py:442](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:442) | trade log/history | shared trade desk | `live` | query `date? limit? tag? outcome?` | `{rows:[...], count}` | generic 500 |
| `PUT` | `/api/v1/execution/{execution_id}` | [unified_api.py:453](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:453) | update forward-tracking fields | shared trade desk | `live` | `ExecutionUpdate` | updated execution row | `404 {"detail":"Execution not found..."}` |
| `PUT` | `/api/v1/execution/{execution_id}/fill` | [unified_api.py:466](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:466) | update fill/fee fields | shared trade desk | `live` | `ExecutionFillUpdate` | updated execution row | `404 {"detail":"Execution not found..."}` |
| `POST` | `/api/v1/execution/{execution_id}/close` | [unified_api.py:478](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:478) | close open position via SignalStack | shared trade desk | `live` | path id | `{id,symbol,direction,exit_price,pnl_dollars,pnl_pct,outcome,execution_fee,webhook_response}` | `404` missing, `400` already closed, `502` broker reject |
| `DELETE` | `/api/v1/execution/{execution_id}` | [unified_api.py:582](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:582) | delete one log row | shared trade desk | `live` | path id | `204 no body` | `404 {"detail":"Execution not found..."}` |
| `DELETE` | `/api/v1/execution/bulk` | [unified_api.py:563](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:563) | bulk delete log rows | shared trade desk | `live` | `{ids?, before_date?}` | `{deleted:n}` | `422 {"detail":"Provide ids and/or before_date"}` |
| `GET` | `/api/v1/execution/analytics` | [unified_api.py:590](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:590) | attribution analytics | shared trade desk | `live` | query `period` | backend returns analytics dict from [trade_desk.py:954](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:954) with `by_tier`, `by_source`, `by_tag`, `period` | generic 500 |
| `GET` | `/api/v1/sizing/{symbol}/{direction}` | [unified_api.py:664](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:664) | ATR sizing helper | shared, but Meridian-data-backed | `live` | path params | `{distance, price}` plus sizing data | `404/500` possible from underlying price lookup |
| `GET` | `/api/v1/portfolio/live` | [unified_api.py:830](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:830) | open positions + live P&L | shared trade desk | `live`, computed not broker-sourced | query `date?` | `{positions:[...], summary:{total_unrealized_pnl,count,winners,losers}, as_of}` | generic 500 |
| `GET` | `/api/v1/accounts` | [unified_api.py:950](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:950) | list account profiles | shared | `live` | none | `{profiles:[...], count}` | generic 500 |
| `GET` | `/api/v1/accounts/{profile_id}` | [unified_api.py:956](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:956) | get one profile | shared | `live` | path id | profile object | `404 {"detail":"Account not found..."}` |
| `POST/PUT/DELETE` | `/api/v1/accounts...` | [unified_api.py:964](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:964), [unified_api.py:969](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:969), [unified_api.py:978](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:978) | account profile CRUD | shared | `live` | `AccountCreate` / `AccountUpdate` | created or updated profile, or `204` delete | `404` on missing profile |
| `GET/POST/PUT/DELETE/POST` | `/api/v1/reports...` | [unified_api.py:620](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:620) onward | report config CRUD + test send | shared | `partial` / `experimental` | report config payloads | list `{reports,count}`, single config, delete ack, test-send result | validation errors, 404 on missing report |

**LANE / SOURCE TRUTH**

| lane | live source | adapter | real row source | row identity | provenance / freshness | native fields | status |
|---|---|---|---|---|---|---|---|
| `S1` | `scorer_predictions` in `/Users/sjani008/SS/Advance/data_cache/signalstack_results.db` + latest `convergence_*.json` in `/Users/sjani008/SS/Advance/evening_results` | [s1_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py) | DB rows are deduped by `(ticker,direction)` using max `scorer_prob`; convergence merged by `(ticker,direction)` | `s1:{ticker}:{LONG|SHORT}:{run_date}:scorer` at [s1_adapter.py:235](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:235) | `as_of` is synthetic via [s1_adapter.py:76](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:76), convergence file chosen by **mtime** at [s1_adapter.py:111](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:111), `partial_data` true when convergence missing | top-level + `native`: `p_tp`, `nn_p_tp`, `scorer_prob`, `convergence_score`, `n_strategies_agree`, `strategy`, `volume_ratio`, `strategies[]` | `live` |
| `Meridian` | `shortlist_daily` + joined `predictions_daily` + `daily_bars` fallback from `/Users/sjani008/SS/Meridian/data/v2_universe.db` | [meridian_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py) | `shortlist_daily` rows for latest `date`; `LEFT JOIN predictions_daily` for LGBM probs; price resolved with shortlist, then predictions, then latest `daily_bars.close <= date` | `meridian:{ticker}:{LONG|SHORT}:{date}` at [meridian_adapter.py:59](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:59) | `as_of` is synthetic via [meridian_adapter.py:39](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:39); TCN fallback `0.5` is converted to `None` at [meridian_adapter.py:49](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:49) | top-level + `native`: `tcn_score`, `factor_rank`, `final_score`, `residual_alpha`, `predicted_return`, `beta`, `rank`, `m_lgbm_long_prob`, `m_lgbm_short_prob` | `live` |
| `Vanguard` | no live candidate source in unified contract | [vanguard_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py) | none; intended future source is `vanguard_shortlist` | none | none | field registry exposes `v_lgbm_long_prob`, `v_lgbm_short_prob`, `v_tcn_long_score`, `v_tcn_short_score`, `spread_proxy`, `session_phase`, `gap_pct`, but adapter returns no rows | `placeholder` |

**COMBINED MODE FINDINGS**
- Merge logic:
  - Backend combined mode in [unified_api.py:260](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:260)-[265](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:265) is simple concatenation of adapter outputs.
  - Today that means `Meridian + S1 + empty Vanguard`, not a true three-lane merge.
- Sort/filter behavior:
  - API supports `sort` and object-style `filters` query params.
  - Current `/candidates` UI does not use those server features. It fetches rows once at [unified-candidates-client.tsx:387](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:387) and then filters/sorts client-side at [unified-candidates-client.tsx:199](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:199) and [unified-candidates-client.tsx:228](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:228).
- Field availability:
  - Combined mode auto-adds `source` to visible columns at [unified-candidates-client.tsx:166](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:166) and [unified-candidates-client.tsx:183](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:183).
  - Many fields are lane-specific. `final_score` is Meridian-only; `scorer_prob` is S1-only; Vanguard-native fields are currently phantom.
- Null handling:
  - Missing source-specific fields remain null and render as `—`.
  - That is honest at the cell level, but misleading at the contract level because the registry suggests those fields are generally available.
- Misleading uniformity:
  - Backend rows include both flat native fields and a nested `native` dict.
  - Frontend normalizes rows into core fields plus `native` only at [unified-api.ts:107](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:107), which hides some top-level metadata such as S1 `partial_data`.
- Freshness drift:
  - `as_of` is not a persisted source timestamp. It is synthesized in adapters with hardcoded ET offsets.

**USERVIEWS FINDINGS**

| route(s) | storage mechanism | object schema | CRUD status | gaps |
|---|---|---|---|---|
| `GET/POST/PUT/DELETE /api/v1/userviews...` | `userviews` SQLite table in `/Users/sjani008/SS/Vanguard/data/vanguard_universe.db` at [userviews.py:22](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:22) | stored columns: `id,name,source,direction,filters,sorts,grouping,visible_columns,column_order,display_mode,is_system,created_at,updated_at` | `live` | timestamps use `datetime('now')`; no schema versioning |
| same | JSON stored as TEXT for `filters, sorts, grouping, visible_columns, column_order` | serialized to API via [_serialize_view](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:236) into parsed arrays/objects | `live` | backend accepts loose `Any`; no strict server validation against field registry |
| system views | seeded in [userviews.py:40](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:40) and reseeded on startup by [seed_system_views](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:88) | only 3 canonical system views: `sys_s1_all`, `sys_meridian_all`, `sys_combined` | `live` | older docs/tests expecting larger system-view sets are stale |
| frontend use | fetched at [unified-api.ts:157](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:157), managed inside [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx) | frontend type is [Userview](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts:59) | `live` | no separate backend-executed view query; userviews only save presentation/query state, not server-side materialization |

**TRADES FINDINGS**

| surface | current truth |
|---|---|
| preview path | UI calls `POST /api/v1/execute` with `preview_only=true` via [unified-api.ts:193](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:193). Backend path is [unified_api.py:432](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:432) -> [trade_desk.py:605](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:605) -> [_evaluate_trade_batch](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:465). |
| execute path | same endpoint with `preview_only=false`; approved trades go to [signalstack_adapter.py:90](/Users/sjani008/SS/Vanguard/vanguard/execution/signalstack_adapter.py:90) through [trade_desk.py:674](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:674). |
| log source | live UI log is `execution_log` via [trade_desk.py:283](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:283) and [trade_desk.py:744](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:744). |
| portfolio source | live portfolio is derived from `execution_log` open rows plus yfinance/Alpaca fallback in [unified_api.py:830](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:830). It is not broker-sourced and does not use `vanguard_portfolio_state`. |
| current UI path | `/trades` -> [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx) -> [unified-api.ts](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts) -> unified API. This is the current live truth. |
| duplicate execution paths | parallel staged path exists in [bridge.py](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py) and [execute_daily_picks.py](/Users/sjani008/SS/Vanguard/scripts/execute_daily_picks.py). It writes `vanguard_execution_log`, not `execution_log`. |
| duplicate log tables / risks | `execution_log` vs `vanguard_execution_log` means two different “truths” for fills/history can diverge immediately. |
| which path should be treated as current live truth | For contract writing: treat `unified_api.py` + `trade_desk.py` + `execution_log` as current live truth, because that is what the merged UI is wired to. Treat `bridge.py` + `vanguard_execution_log` as staged/parallel, not current UI truth. |

**FRONTEND SURFACE MAP**

| route | page component | client component | data dependencies | lane relevance |
|---|---|---|---|---|
| `/` | [app/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/page.tsx) | [dashboard-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/dashboard-client.tsx) | legacy `lib/api.ts` Meridian endpoints + mock data | Meridian legacy shell, not unified-v2-first |
| `/candidates` | [app/candidates/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/candidates/page.tsx) | [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx) | `getUnifiedCandidates`, `getUserviews`, field registry, create/update/delete userview, `fetchSizing` | primary unified S1 + Meridian surface; Vanguard exposed but empty |
| `/trades` | [app/trades/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/trades/page.tsx) | [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx) | `executeTrades`, execution log, analytics, portfolio/live, accounts, update/delete/fill/close, `fetchSizing`, local trade-desk store | current live operator path |
| `/reports` | [app/reports/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/reports/page.tsx) | [reports-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/reports-client.tsx) | reports CRUD/test-send via `lib/api.ts` re-exporting unified API | shared, secondary |
| `/model` | [app/model/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/model/page.tsx) | [model-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/model-client.tsx) | legacy Meridian `getModelFactors/getModelHealth/getTrackingSummary` | Meridian-only, not unified-v2 |
| `/settings` | [app/settings/page.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/app/settings/page.tsx) | [settings-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/settings-client.tsx) | mixed legacy `getApiStatus/getSettings/subscribeApiStatus` plus unified account profile calls | mixed legacy + shared |
| nav shell | [app-shell.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/app-shell.tsx) | same | route/nav only | shell is daily-console oriented, no dedicated Vanguard route |

Additional frontend contract truths:
- Candidate workspace state lives monolithically inside [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx): source selector, filters, sorts, grouping, columns, display mode, userviews, and detail drawer all live there.
- Trades workspace state lives monolithically inside [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx): order cards, preview modal, analytics cards, log table, portfolio table, edit modal.
- API client normalization lives in [unified-api.ts](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts).
- UI types live in [types.ts](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts).

**CONTRACT DRIFT REPORT**

| issue | file(s) | severity | recommended contract stance |
|---|---|---|---|
| Vanguard is exposed as a live source, but adapter is still placeholder | [vanguard_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py), [field_registry.py](/Users/sjani008/SS/Vanguard/vanguard/api/field_registry.py), [unified_api.py](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py), [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx) | `high` | `hide for now` or mark explicitly `placeholder/read-only` in v2 contract |
| `Vanguard_CURRENT_STATE.md` says V5/V6 are not built, but code exists | [Vanguard_CURRENT_STATE.md](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md), [vanguard_selection.py](/Users/sjani008/SS/Vanguard/stages/vanguard_selection.py), [vanguard_risk_filters.py](/Users/sjani008/SS/Vanguard/stages/vanguard_risk_filters.py) | `high` | `reshape` docs to “implemented but not live-wired” |
| Unified health endpoint still says “V5 Strategy Router not built” | [unified_api.py:123](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:123) | `medium` | `reshape` to reflect “adapter placeholder / shortlist absent” |
| Current live execute contract is `approved/rejected/summary`, not old flat list semantics | [trade_desk.py:628](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py:628), [types.ts:173](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts:173), failing tests in [test_unified_api.py](/Users/sjani008/SS/Vanguard/tests/test_unified_api.py) | `high` | `preserve` current shape and update docs/tests |
| Frontend `CandidateRow.native` type is too narrow for actual backend payloads | [types.ts:15](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts:15), [s1_adapter.py:262](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:262) | `medium` | `reshape` type to allow arrays/objects in `native` or split richer detail payload |
| Frontend normalization drops some backend fields like S1 `partial_data` | [unified-api.ts:107](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:107), [s1_adapter.py:244](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:244) | `medium` | `reshape` if provenance flags belong in v2 contract; otherwise deprecate explicitly |
| `as_of` is synthetic adapter output, not a persisted source timestamp | [s1_adapter.py:76](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:76), [meridian_adapter.py:39](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:39) | `medium` | `preserve` but document as synthetic display freshness, not event time |
| Combined mode implies more normalization than actually exists | [unified_api.py:260](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:260), [unified-candidates-client.tsx:199](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:199) | `high` | `reshape` combined contract as “concatenated multi-source view with nullable lane-specific fields” |
| Duplicate execution/logging truth | [trade_desk.py](/Users/sjani008/SS/Vanguard/vanguard/api/trade_desk.py), [bridge.py](/Users/sjani008/SS/Vanguard/vanguard/execution/bridge.py) | `high` | `deprecate` one path from v2 contracts; keep only UI-wired path in scope |
| System views drift from older expectations | [userviews.py:40](/Users/sjani008/SS/Vanguard/vanguard/api/userviews.py:40), failing tests in [test_unified_api.py](/Users/sjani008/SS/Vanguard/tests/test_unified_api.py) | `medium` | `preserve` 3 seeded canonical views in v2 contract |
| Dashboard/model/settings still depend partly on legacy Meridian API | [dashboard-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/dashboard-client.tsx), [model-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/model-client.tsx), [settings-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/settings-client.tsx) | `medium` | `out of scope` for unified-v2 unless separately contractized |

**RECOMMENDED CONTRACT SCOPE**
- `in scope now`
  - `UNIFIED_API_CONTRACT_v2`
    - `/api/v1/health`
    - `/api/v1/field-registry`
    - `/api/v1/candidates`
    - `/api/v1/candidates/{row_id}`
    - `/api/v1/userviews*`
    - `/api/v1/picks/today`
    - `/api/v1/execute` with `preview_only`
    - `/api/v1/execution/*`
    - `/api/v1/sizing/*`
    - `/api/v1/portfolio/live`
    - `/api/v1/accounts*`
    - `/api/v1/reports*` if reports page stays in current UI scope
  - `UNIFIED_BACKEND_DATA_CONTRACT_v2`
    - S1 source truth = `scorer_predictions` + latest convergence JSON by mtime
    - Meridian source truth = `shortlist_daily` + `predictions_daily` + `daily_bars` fallback
    - shared trade truth = `execution_log`, `userviews`, `account_profiles`
  - `UNIFIED_FRONTEND_CONTRACT_v2`
    - `/candidates`
    - `/trades`
    - `/reports`
    - route/client/API/type surfaces in [unified-candidates-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx), [trades-client.tsx](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/trades-client.tsx), [unified-api.ts](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts), [types.ts](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts)
- `out of scope now`
  - live Vanguard candidate rows
  - `vanguard_shortlist` / `vanguard_tradeable_portfolio` contracts as operator-facing APIs
  - `bridge.py` / `vanguard_execution_log`
  - empty [vanguard_orchestrator.py](/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py)
  - empty [mt5_adapter.py](/Users/sjani008/SS/Vanguard/vanguard/execution/mt5_adapter.py)
  - legacy dashboard/model surfaces unless separately rewritten into unified contracts
- `experimental / read-only`
  - `/api/v1/reports*`
  - `/api/v1/picks/today`
  - account profile tuning fields for pre-execution gate
- `deferred`
  - true Vanguard lane contract once `vanguard_adapter` reads a real shortlist
  - broker-truth portfolio/state contract
  - orchestrator/session lifecycle contract
  - de-duplication of execution and logging paths

No files were modified.
**EXECUTIVE SUMMARY**
- I compared the live adapter code against [AGENTS.md](/Users/sjani008/SS/Meridian/AGENTS.md), [MERIDIAN_CURRENT_STATE.md](/Users/sjani008/SS/Meridian/MERIDIAN_CURRENT_STATE.md), [STAGE_5_SELECTION_SPEC.md](/Users/sjani008/SS/Meridian/docs/STAGE_5_SELECTION_SPEC.md), [Vanguard_CURRENT_STATE.md](/Users/sjani008/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md), [qa_report_shared_contract.md](/Users/sjani008/SS/Advance/qa_report_shared_contract.md), and [BACKEND_CONTRACT.md](/Users/sjani008/SS/Vanguard/docs/UI%20Docs/BACKEND_CONTRACT.md).
- **S1 adapter is not a pure adapter.** It is a **truth-changing transformation layer**. The main problem is [s1_adapter.py:175](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:175), which collapses raw `scorer_predictions` rows by `(ticker, direction)` and then independently takes `MAX(...)` across multiple fields. That can manufacture a synthetic candidate row that never existed in the source table.
- **Meridian adapter is closer to a real contract adapter, but still not pure.** It is an **enriching/shaping layer** with some risky rewrites: joins `predictions_daily`, rewrites `tcn_score=0.5 -> None`, injects fallback prices from other tables, and reorders rows by `final_score`.
- **Unified API candidate flow is mostly pass-through**, but combined mode in [unified_api.py:260](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:260)-[275](/Users/sjani008/SS/Vanguard/vanguard/api/unified_api.py:275) concatenates heterogeneous adapter outputs and can apply extra filter/sort/pagination transforms.
- **Frontend normalization loses truth.** The biggest losses are in [unified-api.ts:107](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:107): top-level backend metadata like `partial_data` gets dropped, null semantics are narrowed, and `CandidateRow.native` in [types.ts:15](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts:15) is typed too narrowly for actual backend payloads like S1 `strategies[]`.
- Bottom line:
  - **S1 adapter is not trustworthy as a pure adapter**
  - **Meridian adapter is usable but not pure**
  - The new contracts must explicitly distinguish **source truth**, **adapter enrichment**, and **frontend-normalized truth**

**S1 ADAPTER FINDINGS**

**Source-of-truth mapping**
- Raw sources:
  - DB: `/Users/sjani008/SS/Advance/data_cache/signalstack_results.db`, table `scorer_predictions`, used at [s1_adapter.py:169](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:169)-[201](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:201)
  - File: latest `convergence_*.json` in `/Users/sjani008/SS/Advance/evening_results`, chosen by **mtime** at [s1_adapter.py:106](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:106)-[112](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:112)
- Date/run selection:
  - defaults to `MAX(run_date)` from `scorer_predictions` at [s1_adapter.py:168](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:168)-[172](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:172)
- Row identity:
  - `row_id = s1:{ticker}:{direction}:{run_date}:scorer` at [s1_adapter.py:235](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:235)
- Freshness logic:
  - synthetic `as_of` from [s1_adapter.py:76](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:76)-[77](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:77), not a source timestamp
  - convergence freshness is file mtime, not DB-linked provenance

**S1 mismatch table**

| source behavior expected | actual adapter behavior | exact file/line | effect on candidate truth | severity |
|---|---|---|---|---|
| Raw `scorer_predictions` rows should be represented faithfully | Groups by `(ticker, normalized direction)` and collapses rows | [s1_adapter.py:174](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:174)-[199](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:199) | Multiple real candidate rows are compressed into one | `high-risk` |
| Field values should come from a single real source row | Uses `MAX(scorer_prob)`, `MAX(p_tp)`, `MAX(nn_p_tp)`, `MAX(price)`, `MAX(regime)`, `MAX(sector)`, `MAX(gate_source)` independently | [s1_adapter.py:182](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:182)-[189](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:189) | Can create a synthetic hybrid row that never existed in source truth | `high-risk` |
| Strategy-level detail should survive if multiple raw rows exist | `GROUP_CONCAT(strategy)` is collected as `strategies_raw` but then ignored | [s1_adapter.py:189](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:189), [s1_adapter.py:251](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:251)-[263](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:263) | Strategy provenance is partially lost; one string `gate_source` is shown instead | `high-risk` |
| Direction normalization should be shape-only | BUY/SELL normalized to LONG/SHORT | [s1_adapter.py:177](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:177)-[181](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:181), [s1_adapter.py:38](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:38)-[44](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:44) | Harmless contract shaping | `harmless` |
| Convergence should come from latest intended artifact | Loads latest file by **mtime** and merges by `(ticker,direction)` | [s1_adapter.py:97](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:97)-[140](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:140), [s1_adapter.py:222](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:222)-[226](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:226) | Acceptable if contract says “latest by mtime”, but still a multi-source merge | `questionable` |
| Missing convergence should preserve absence explicitly | Missing convergence becomes `convergence_score=0.0` and `partial_data=true` | [s1_adapter.py:244](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:244), [s1_adapter.py:249](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:249) | Default substitution may hide distinction between “real zero” and “missing” unless `partial_data` survives | `questionable` |
| Price should remain source-local unless explicitly documented | If DB price is null/0, falls back to Meridian `daily_bars` | [s1_adapter.py:218](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:218)-[220](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:220), [s1_adapter.py:80](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:80)-[94](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:94) | Cross-system price substitution changes provenance and freshness | `truth-changing` |
| Adapter should not silently rank unless contract says so | Query ends with `ORDER BY scorer_prob DESC` | [s1_adapter.py:199](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:199) | Output order differs from raw table order | `questionable` |
| Freshness should be source-driven | `as_of` is synthetic `run_date + 17:16 ET` | [s1_adapter.py:76](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:76)-[77](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:77), [s1_adapter.py:240](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:240) | Not a real source timestamp; display freshness only | `questionable` |
| Tier should reflect source truth, not invent meaning | Adapter derives `tier_*` from thresholds | [s1_adapter.py:47](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:47)-[73](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:73), [s1_adapter.py:228](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:228) | Adds a useful derived field, but this is not raw S1 source truth | `harmless shaping` if documented |

**S1 verdict**
- The S1 adapter is **not** a pure contract adapter.
- Its most serious issue is the SQL collapse pattern. It does not merely normalize or enrich; it can **invent composite candidate rows** and erase strategy-level source truth.

**MERIDIAN ADAPTER FINDINGS**

**Source-of-truth mapping**
- Raw sources:
  - `shortlist_daily` is the primary source at [meridian_adapter.py:153](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:153)
  - `predictions_daily` is joined for LGBM at [meridian_adapter.py:154](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:154)-[155](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:155)
  - `daily_bars` is used as a price fallback subquery at [meridian_adapter.py:138](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:138)-[146](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:146)
- Date selection:
  - defaults to `MAX(date)` from `shortlist_daily` at [meridian_adapter.py:118](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:118)-[122](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:122)
- Row identity:
  - `row_id = meridian:{ticker}:{direction}:{date}` at [meridian_adapter.py:59](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:59)
- Freshness logic:
  - synthetic `as_of` from [meridian_adapter.py:39](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:39)-[41](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:41)
  - no explicit partial/provenance flag when `predictions_daily` join is absent

**Meridian mismatch table**

| source behavior expected | actual adapter behavior | exact file/line | effect on candidate truth | severity |
|---|---|---|---|---|
| `shortlist_daily` should be the base truth | Reads `shortlist_daily`, then enriches with `predictions_daily` and fallback prices | [meridian_adapter.py:124](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:124)-[156](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:156) | Mostly acceptable, but adapter is no longer a pure projection of shortlist | `questionable` |
| Join assumptions should be explicit and safe | Joins `predictions_daily` on `(date,ticker)` only | [meridian_adapter.py:154](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:154)-[155](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:155) | If Stage 4 is missing/stale, LGBM fields disappear; if table missing, adapter returns `[]` | `high-risk` |
| Price should preserve source provenance | Uses `COALESCE(shortlist.price, predictions.price, latest daily_bars.close<=date)` | [meridian_adapter.py:135](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:135)-[147](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:147) | Truth is changed from shortlist price to inferred price chain | `truth-changing` |
| TCN value should preserve model output semantics | Rewrites exact `0.5` to `None` | [meridian_adapter.py:47](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:47)-[51](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:51) | Good if 0.5 is only fallback; risky if genuine 0.500 scores can exist | `questionable` |
| Adapter should not change ordering unless explicit | Query ends with `ORDER BY final_score DESC` | [meridian_adapter.py:162](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:162) | Re-ranks output relative to raw shortlist table order/rank semantics | `questionable` |
| Missing values should remain distinguishable | Sector defaults to `UNKNOWN`, rank defaults `0`, price defaults `0.0`, beta/residual/pred_return default to `0.0` | [meridian_adapter.py:63](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:63), [meridian_adapter.py:66](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:66), [meridian_adapter.py:71](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:71)-[77](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:71) | Null semantics are compressed into defaults | `questionable` |
| Tier should reflect source truth, not invent meaning | Adapter derives `tier_meridian_*` from thresholds | [meridian_adapter.py:28](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:28)-[36](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:36), [meridian_adapter.py:65](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:65) | Useful presentation field; not raw shortlist truth | `harmless shaping` if documented |
| Freshness should be source-driven | `as_of` is synthetic `date + 17:00 ET` | [meridian_adapter.py:39](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:39)-[41](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:41) | Display freshness only, not real source timestamp | `questionable` |
| Adapter should not add/suppress rows | No dedup/grouping; row count mirrors shortlist rows for selected date | [meridian_adapter.py:164](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:164)-[172](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py:172) | Good; no row collapse | `harmless` |

**Meridian verdict**
- Meridian adapter is **not pure**, but it is much closer to a legitimate contract adapter than S1.
- It is mainly an **enrichment/shaping layer** around `shortlist_daily`.
- The riskiest parts are:
  - reliance on `predictions_daily` existence
  - fallback price substitution
  - using value-based inference (`0.5 -> None`) for TCN provenance

**FRONTEND TRUTH-LOSS FINDINGS**

| field / behavior | backend truth | frontend behavior | impact | severity |
|---|---|---|---|---|
| `partial_data` | S1 adapter emits top-level `partial_data` at [s1_adapter.py:244](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:244) | dropped by [unified-api.ts:109](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:109)-[124](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:124) because `native` is used and top-level extras are ignored | UI cannot distinguish complete vs partial S1 rows | `high` |
| `native` typing | S1 backend sends `native.strategies` as array at [s1_adapter.py:262](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:262) | `CandidateRow.native` type forbids arrays in [types.ts:15](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/types.ts:15) | frontend type contract is false; future code can silently mishandle backend truth | `medium` |
| null price semantics | backend may send null/0 only in failure cases | frontend forces `price: Number(r.price ?? 0)` at [unified-api.ts:118](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:118) | missing price becomes numeric 0 | `medium` |
| missing tier semantics | backend may omit tier only on bug/placeholder | frontend forces `"—"` at [unified-api.ts:120](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:120) | obscures “missing backend field” vs legitimate untiered | `low` |
| missing source semantics | backend should always send source | frontend defaults to `meridian` at [unified-api.ts:115](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:115) | could misclassify malformed rows as Meridian | `medium` |
| missing side semantics | backend should always send side | frontend defaults to `LONG` at [unified-api.ts:117](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:117) | malformed rows can silently become LONG | `medium` |
| flat source-native fields | backend rows include top-level native fields for filtering and debugging | frontend canonical row keeps only core fields + `native` at [unified-api.ts:113](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:113)-[124](/Users/sjani008/SS/Meridian/ui/signalstack-app/lib/unified-api.ts:124) | some top-level provenance/debug fields are lost unless duplicated inside `native` | `medium` |
| combined mode truth | backend combined is lane concat with nullable lane-specific fields | frontend applies local filters/sorts at [unified-candidates-client.tsx:199](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:199)-[228](/Users/sjani008/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx:228) and can present one table over heterogeneous rows | encourages “shared table = shared meaning” when meaning is lane-specific | `high` |
| field registry promises | registry publishes Vanguard-native fields at [field_registry.py:139](/Users/sjani008/SS/Vanguard/vanguard/api/field_registry.py:139)-[174](/Users/sjani008/SS/Vanguard/vanguard/api/field_registry.py:174) | frontend uses registry dynamically | UI exposes fields for a lane with no live rows | `medium` |

**SPEC VS ADAPTER MISMATCHES**
- S1 spec/contract says:
  - latest convergence file must be chosen by mtime
  - side normalized BUY→LONG, SELL→SHORT
  - missing convergence gives `convergence_score=0.0` and `partial_data=true`
  - price fallback allowed
  - all scorer rows for latest run date must be returned
- S1 adapter actually:
  - matches mtime rule and direction normalization
  - matches missing convergence rule
  - **does not** return raw scorer rows; it **collapses** them by `(ticker,direction)` with per-column `MAX(...)`
  - drops some raw strategy detail
- Meridian spec/current-state says:
  - `shortlist_daily` is the Stage 5 output truth
  - Stage 5 raw output is prediction/residual/rank-oriented
  - unified contract expects LGBM join, TCN fallback nulling, tier derivation, and non-zero price fallback
- Meridian adapter actually:
  - matches the unified contract on TCN nulling, LGBM join, tier derivation, and price fallback
  - goes beyond raw Stage 5 truth by enriching rows with Stage 4 data and fallback values
  - sorts by `final_score`, which is a presentation/ranking choice, not raw shortlist truth
- Unified contract spec says combined mode should never fake a unified score
- Current stack:
  - avoids an explicit fake `primary_score`
  - but still compresses heterogeneous fields into one table and lets frontend combined mode operate across them as if they were peers
- Verdict on mismatches:
  - S1 collapse logic: **wrong**
  - Meridian enrichment logic: **acceptable only if explicitly contractized**
  - frontend loss of `partial_data` and native-type mismatch: **risky and wrong for contract fidelity**

**ADAPTER INTEGRITY TABLE**

| adapter/layer | source truth | current behavior | truth-changing operations | severity | verdict |
|---|---|---|---|---|---|
| `s1_adapter.py` | raw `scorer_predictions` rows + latest convergence JSON | dedupes, merges, defaults, re-ranks, derives tiers, synthesizes timestamps | grouping/collapse, per-field `MAX`, loss of strategy detail, cross-system price fallback, synthetic `as_of` | `high` | **truth-changing transformation layer** |
| `meridian_adapter.py` | `shortlist_daily` rows for latest date | enriches with `predictions_daily`, fallback prices, TCN null rewrite, derived tiers, sorted output | join inflation, price substitution, null rewrite, synthetic `as_of`, output reorder | `medium` | **mostly shaping/enrichment, not pure** |
| `unified_api.py` candidate flow | adapter outputs | concatenates by source, optional filter/sort/paginate, detail refetch by row_id | combined concat, optional extra filtering/sorting, pagination | `medium` in combined mode | **mostly pass-through, but combined mode is a transformation layer** |
| frontend normalization in `unified-api.ts` | backend candidate rows | canonicalizes to reduced `CandidateRow`, drops some top-level fields, coerces defaults | field dropping, null coercion, default source/side/price/tier semantics | `medium` | **truth-losing normalization layer** |
| field/type layer | backend field registry + runtime rows | static TS types narrower than runtime payload | interpretation drift, phantom fields, weak provenance typing | `medium` | **contract drift layer** |

**FINAL VERDICT**
- **Is S1 adapter trustworthy as a pure adapter?**
  - No.
  - It is a **truth-changing transformation layer** because it collapses raw multi-row source truth into synthetic single rows.
- **Is Meridian adapter trustworthy as a pure adapter?**
  - No, but it is much closer.
  - It is a **contract-shaping enrichment layer** around `shortlist_daily`, not a raw truth adapter.
- **What must be changed immediately?**
  - The S1 collapse query in [s1_adapter.py:175](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:175)-[199](/Users/sjani008/SS/Vanguard/vanguard/api/adapters/s1_adapter.py:199) must not be treated as “pure source truth” in any new contract.
  - Frontend must not silently drop `partial_data`.
  - `CandidateRow.native` type must be widened or contractized correctly.
- **What can stay?**
  - Direction normalization in S1.
  - Meridian `predictions_daily` LGBM enrichment.
  - Tier derivation in both adapters if clearly documented as derived display fields.
  - Price fallback behavior only if contracts explicitly call it out as cross-source substitution.
- **What should be documented explicitly in the new contracts?**
  - S1 row is **not** a raw `scorer_predictions` row.
  - Meridian row is **not** a raw `shortlist_daily` row.
  - `as_of` is synthetic in both adapters.
  - Combined mode is concatenated heterogeneous rows, not normalized cross-source truth.
  - Frontend consumes a reduced normalized candidate model, not the raw backend row.

**REQUIRED CONTRACT RULES**
- Define three layers explicitly:
  - `source truth`
  - `adapter output`
  - `frontend-normalized view`
- For S1:
  - contract must state whether the unified row is allowed to collapse multiple raw scorer rows
  - if yes, the collapse algorithm must be explicitly defined
  - if no, the adapter must change later, but for now the contract must label current behavior as truth-changing
- For Meridian:
  - contract must state that unified rows are `shortlist_daily` plus optional `predictions_daily` enrichment plus fallback price resolution
- For both adapters:
  - `as_of` must be documented as synthetic display freshness, not source event time
  - derived `tier` must be documented as presentation logic, not source table truth
- For frontend:
  - `partial_data` provenance must be preserved if it is contract-relevant
  - `native` type must allow arrays/structured values where backend emits them
  - no field may be silently defaulted to a misleading business value without contract approval
- For combined mode:
  - contract must explicitly state that lane-specific fields are nullable and non-comparable across lanes
  - contract must forbid implying a shared score unless a real cross-lane metric exists

No files were modified.