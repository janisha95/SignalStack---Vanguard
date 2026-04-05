# Vanguard — Build Roadmap

## Current Phase: Phase 0 — Executor Bridge

**Priority:** Build the executor FIRST. Place Meridian + S1 daily picks
on TTP $1M demo via SignalStack starting Monday Mar 31.

## Timeline

| Week | Phase | What | Status |
|------|-------|------|--------|
| Mar 31 - Apr 4 | **Phase 0** | V7.1 Executor Bridge (SignalStack + Telegram) | 🔨 Building |
| Apr 7 - Apr 11 | Phase 1 | V1 + V2 Data Pipeline | ⬜ |
| Apr 14 - Apr 18 | Phase 2 | V3 + V4A + V4B Features + ML | ⬜ |
| Apr 21 - Apr 25 | Phase 3 | V5 + V6 Selection + Risk | ⬜ |
| Apr 28 - May 2 | Phase 4 | V7 Full Orchestrator | ⬜ |
| May 5 - May 9 | Phase 5 | IBKR + MT5 + FTMO Multi-Asset | ⬜ |
| May 12+ | Phase 6 | LLM Strategy + Dashboard | ⬜ |

## Phase 0 Build Order (This Week)

```
Day 1: signalstack_adapter.py + telegram_alerts.py + bridge.py
Day 2: execute_daily_picks.py (Meridian→executor, S1→executor)
Day 3: First live demo execution on TTP
Day 4-5: Iterate, fix, tune position sizing
```

## Dependencies

```
Phase 0 (Executor) → standalone (reads Meridian/S1 picks, writes to TTP demo)
Phase 1 (V1+V2) → Alpaca API key (have), IBKR $500 funding (Monday)
Phase 2 (V3+V4) → Phase 1 bars in DB
Phase 3 (V5+V6) → Phase 2 features + predictions
Phase 4 (V7) → Phase 3 + Phase 0 executor (already built)
Phase 5 (Multi-asset) → IBKR data subscriptions + MT5 terminal
Phase 6 (LLM) → News feed + Claude API
```

## Key Decisions (Locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Project name | Vanguard | Multi-asset intraday system |
| File naming | vanguard_*.py | Clean namespace |
| DB | vanguard_universe.db | Isolated from Meridian/S1 |
| ML models | 2×LightGBM + TCN dual-head | LGBM fast (Mac), TCN for temporal (GPU) |
| Selection | Strategy Router (13 strategies, 6 asset classes) | Asset-class-specific |
| Execution | SignalStack (TTP) + MT5 API (FTMO) | No custom broker integration |
| Bar interval | 5m canonical, 1h derived | SMC HTF features need 1H |
| TBM | +0.30% TP, -0.15% SL, 30 min horizon | 2:1 R/R, 33.3% breakeven WR |
| Risk | Multi-account prop firm (TTP + FTMO) | Config-driven, no code changes to add accounts |
| Execution default | OFF (forward tracking) | Trust before execution |

## Spec Approval Status

| Spec | Status |
|------|--------|
| V1 Cache | ✅ APPROVED |
| V2 Prefilter | ✅ APPROVED |
| V3 Factor Engine | ✅ APPROVED |
| V4A Training Backfill | ✅ APPROVED |
| V4B Model Trainer | ✅ APPROVED |
| V5 Selection (Strategy Router v2) | ✅ APPROVED |
| V6 Risk Filters (TTP platform data) | ✅ APPROVED |
| V7 Orchestrator + Executor | ✅ APPROVED |
| V7.1 Executor Only | ✅ APPROVED |
| Supporting Specs (repo, DB, config, time, helpers) | ✅ APPROVED |
| Build Plan | ✅ APPROVED |

## For Claude Code / Codex

**Current task: Build V7.1 Executor**

1. Read `AGENTS.md` first
2. Read `ROADMAP.md` (this file) for current phase
3. Read `VANGUARD_STAGE_V7_1_EXECUTOR_SPEC.md` for the build spec
4. Read `VANGUARD_SUPPORTING_SPECS.md` for DB schema (vanguard_execution_log table)
5. Build, test, validate against acceptance criteria
6. Never modify files outside `~/SS/Vanguard/`
