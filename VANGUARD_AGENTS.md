# Vanguard — Multi-Asset Intraday Trading System

## Project Overview

Vanguard is a multi-asset, multi-prop-firm, ML-driven intraday trading system. It scans US equities (TTP), forex, indices, metals, crypto, and commodities (FTMO) on 5-minute bars, scores candidates using a 3-model ML ensemble (2×LightGBM + TCN dual-head), selects trades via a Strategy Router with 13 asset-class-specific strategies, and executes via SignalStack (TTP) or MT5 API (FTMO).

**Owner:** Shan Jani
**Architect:** Claude Opus 4.6 (Anthropic)
**Predecessor:** Meridian (daily equities), S1 (daily ML-gated strategies)

## Architecture

```
V1 Cache → V2 Prefilter → V3 Features → V4B ML → V5 Selection → V6 Risk → V7 Orchestrator+Executor
```

7 stages, executed in a loop every 5 minutes during market hours (09:35-15:25 ET).

## Repo Structure

```
~/SS/Vanguard/
├── AGENTS.md                           # THIS FILE — read first
├── ROADMAP.md                          # Build plan, timeline, dependencies
├── docs/
│   ├── VANGUARD_STAGE_V1_SPEC.md
│   ├── VANGUARD_STAGE_V2_SPEC.md
│   ├── VANGUARD_STAGE_V3_SPEC.md
│   ├── VANGUARD_STAGE_V4A_SPEC.md
│   ├── VANGUARD_STAGE_V4B_SPEC.md
│   ├── VANGUARD_STAGE_V5_SPEC.md       # Strategy Router (v2)
│   ├── VANGUARD_STAGE_V6_SPEC.md       # TTP platform data
│   ├── VANGUARD_STAGE_V7_SPEC.md       # Orchestrator + Execution Bridge
│   ├── VANGUARD_STAGE_V7_1_EXECUTOR_SPEC.md  # Executor-only (build first)
│   ├── VANGUARD_SUPPORTING_SPECS.md    # Repo, DB, config, time, helpers
│   ├── VANGUARD_BUILD_PLAN.md
│   └── PROJECT_VANGUARD_HANDOFF.md
├── stages/
│   ├── vanguard_cache.py               # V1
│   ├── vanguard_prefilter.py           # V2
│   ├── vanguard_factor_engine.py       # V3
│   ├── vanguard_training_backfill.py   # V4A
│   ├── vanguard_model_trainer.py       # V4B
│   ├── vanguard_selection.py           # V5
│   ├── vanguard_risk_filters.py        # V6
│   └── vanguard_orchestrator.py        # V7
├── vanguard/
│   ├── __init__.py
│   ├── strategies/                     # V5 strategy scoring functions
│   ├── factors/                        # V3 feature modules
│   ├── helpers/                        # Shared utilities
│   ├── execution/                      # SignalStack, MT5, Telegram
│   ├── data_adapters/                  # Alpaca, IBKR, MT5 data
│   └── models/                         # Feature contract
├── scripts/
│   ├── execute_daily_picks.py          # Meridian/S1 → executor bridge
│   └── eod_flatten.py                  # Manual flatten command
├── config/
│   ├── vanguard_accounts.json
│   ├── vanguard_strategies.json
│   ├── vanguard_orchestrator_config.json
│   ├── vanguard_execution_config.json
│   ├── vanguard_instrument_specs.json
│   └── vanguard_expected_bars.json
├── data/
│   ├── vanguard_universe.db
│   ├── reports/
│   └── runtime/
├── models/
│   └── vanguard/
└── tests/
```

## Key Principles

1. **Spec before code.** Every stage has a spec. No coding without an approved spec.
2. **Vanguard owns its own DB.** `vanguard_universe.db`. Never write to Meridian or S1 databases.
3. **Multi-asset from day 1.** Equities, forex, indices, metals, crypto, commodities.
4. **Strategy Router, not single-score.** 13 strategies across 6 asset classes. Each produces its own ranked list.
5. **Executor is system-agnostic.** Same bridge serves Meridian, S1, and Vanguard picks.
6. **Execution defaults to OFF.** Forward tracking mode until trust is established.
7. **No beta stripping.** Lesson from Meridian disaster. Edge formula only.
8. **Config-driven.** No hardcoded paths, URLs, times, or credentials in stage files.

## Systems That Feed the Executor

| System | What It Produces | Status |
|---|---|---|
| Meridian | 30 LONG + 30 SHORT daily picks (5-day hold) | Running, automated at 5 PM ET |
| S1 | ML-gated daily picks (NN 73% WR, LGBM 71%) | Running, morning reports at 6:30 AM |
| Vanguard | Intraday picks every 5 min (building) | Phase 0: executor first |

## Environment Variables

```bash
ALPACA_KEY=pk_...
ALPACA_SECRET=sk_...
SIGNALSTACK_WEBHOOK_URL=https://...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
# Future
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
MT5_LOGIN=...
MT5_PASSWORD=...
MT5_SERVER=...
```

## For Claude Code / Codex

When starting any task:
1. Read `AGENTS.md` (this file) first
2. Read `ROADMAP.md` for build order and current phase
3. Read the specific `VANGUARD_STAGE_*_SPEC.md` for the stage you're building
4. Read `VANGUARD_SUPPORTING_SPECS.md` for DB schema, config, repo structure
5. Build, test, validate against the spec's acceptance criteria
6. Never modify files outside `~/SS/Vanguard/`
7. Never import from `~/SS/Meridian/stages/v2_*.py`
8. All DB writes go to `vanguard_universe.db` only
