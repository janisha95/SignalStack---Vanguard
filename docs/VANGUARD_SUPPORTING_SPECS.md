# VANGUARD SUPPORTING SPECS — Combined Document

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 29, 2026

This document contains 5 supporting specs + canonical doc set index.
Combined into one file to avoid spec sprawl.

---

# 1. REPO STRUCTURE

## Root
`~/SS/Vanguard/` (separate from `~/SS/Meridian/`)

## Folder Tree

```
~/SS/Vanguard/
├── stages/
│   ├── vanguard_cache.py               # V1
│   ├── vanguard_prefilter.py           # V2
│   ├── vanguard_factor_engine.py       # V3
│   ├── vanguard_training_backfill.py   # V4A
│   ├── vanguard_model_trainer.py       # V4B
│   ├── vanguard_selection.py           # V5
│   ├── vanguard_risk_filters.py        # V6
│   └── vanguard_orchestrator.py        # V7
│
├── vanguard/
│   ├── __init__.py
│   ├── strategies/                     # V5 strategy scoring functions
│   │   ├── base.py
│   │   ├── smc_confluence.py
│   │   ├── momentum.py
│   │   ├── mean_reversion.py
│   │   ├── risk_reward_edge.py
│   │   ├── relative_strength.py
│   │   ├── breakdown.py               # SHORT ONLY (from S1 BRF)
│   │   ├── session_timing.py
│   │   ├── htf_alignment.py
│   │   ├── liquidity_grab_reversal.py
│   │   ├── session_open.py
│   │   ├── cross_asset.py
│   │   ├── momentum_breakout.py
│   │   └── llm_strategy.py
│   ├── factors/                        # V3 feature modules
│   │   ├── price_location.py
│   │   ├── momentum.py
│   │   ├── volume.py
│   │   ├── market_context.py
│   │   ├── daily_bridge.py
│   │   ├── smc_5m.py
│   │   ├── smc_htf_1h.py
│   │   ├── session_time.py
│   │   └── quality.py
│   ├── helpers/
│   │   ├── clock.py                    # UTC/ET time helpers
│   │   ├── market_sessions.py          # Session boundaries, holidays
│   │   ├── db.py                       # SQLite WAL helpers
│   │   ├── bars.py                     # Bar normalization, aggregation
│   │   ├── quality.py                  # NaN/outlier checks
│   │   ├── normalize.py                # Feature normalization
│   │   ├── regime_detector.py          # 5-feature regime gate
│   │   ├── strategy_router.py          # Asset class → strategy mapping
│   │   ├── consensus_counter.py        # Multi-strategy agreement
│   │   ├── position_sizer.py           # Equity/forex/futures sizing
│   │   ├── portfolio_state.py          # Per-account state management
│   │   ├── correlation_checker.py      # Pairwise correlation
│   │   ├── eod_flatten.py             # EOD close logic
│   │   └── ttp_rules.py              # TTP-specific rules
│   ├── execution/
│   │   ├── bridge.py                   # Execution dispatcher
│   │   ├── signalstack_adapter.py      # SignalStack webhook client
│   │   ├── mt5_adapter.py              # MT5 API client
│   │   └── telegram_alerts.py          # Telegram bot notifications
│   ├── data_adapters/
│   │   ├── alpaca_adapter.py           # Alpaca REST + WebSocket
│   │   ├── ibkr_adapter.py            # IBKR TWS API (ib_async)
│   │   └── mt5_data_adapter.py        # MT5 market data
│   └── models/
│       └── feature_contract.py         # 35 feature definitions
│
├── scripts/
│   ├── execute_daily_picks.py          # Meridian/S1 → executor bridge
│   └── eod_flatten.py                  # Manual flatten command
│
├── config/
│   ├── vanguard_accounts.json          # Prop firm accounts
│   ├── vanguard_strategies.json        # Strategy registry per asset class
│   ├── vanguard_selection_config.json   # V5 thresholds
│   ├── vanguard_orchestrator_config.json # V7 session timing
│   ├── vanguard_execution_config.json   # SignalStack, MT5, Telegram
│   ├── vanguard_instrument_specs.json   # Tick values, lot sizes, pip sizes
│   └── vanguard_expected_bars.json     # Per-asset avg exit bars
│
├── data/
│   ├── vanguard_universe.db            # ALL Vanguard runtime data
│   ├── reports/                        # Daily session reports
│   └── runtime/                        # Runtime state files
│
├── models/
│   └── vanguard/
│       ├── latest/                     # Current model artifacts
│       └── archive/                    # Previous model versions
│
└── tests/
    ├── test_signalstack_adapter.py
    ├── test_mt5_adapter.py
    ├── test_vanguard_selection.py
    ├── test_vanguard_risk.py
    └── test_vanguard_orchestrator.py
```

## Naming Rules
- Every stage entrypoint: `vanguard_*.py`
- Every helper: under `vanguard/` package
- Every config: under `config/`
- Every table: `vanguard_*` prefix
- File naming: `vanguard_cache.py`, NOT `v1_cache.py`

## Forbidden
- No files inside `~/SS/Meridian/` modified by Vanguard
- No imports from `stages/v2_*.py` (Meridian daily stages)
- No imports from S1 stage files
- No writes to `v2_universe.db` or `s1_universe.db`

---

# 2. DB SCHEMA & ISOLATION

## Canonical DB
`data/vanguard_universe.db` — SQLite with WAL mode

## Isolation Rules
- Vanguard reads/writes ONLY `vanguard_universe.db`
- Meridian DB (`v2_universe.db`) is NEVER accessed by Vanguard stages
- S1 DB is NEVER accessed by Vanguard stages
- The ONLY cross-system data flow is via the executor bridge:
  Meridian shortlist → universal trade object → executor bridge
  (read from v2_universe.db by `scripts/execute_daily_picks.py` ONLY)

## Startup Guard
```python
def assert_db_isolation(db_path: str):
    """Every Vanguard stage calls this at startup."""
    assert 'vanguard_universe' in db_path, \
        f"DB isolation violation: {db_path} is not the Vanguard DB"
    assert 'v2_universe' not in db_path, \
        f"DB isolation violation: attempting to use Meridian DB"
```

## Core Tables

### Market Data
```sql
-- V1: 5-minute bars
CREATE TABLE IF NOT EXISTS vanguard_bars_5m (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    asset_class TEXT,
    data_source TEXT,
    PRIMARY KEY (symbol, bar_ts_utc)
);

-- V1: 1-hour bars (derived from 5m)
CREATE TABLE IF NOT EXISTS vanguard_bars_1h (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    PRIMARY KEY (symbol, bar_ts_utc)
);
```

### Pipeline Runtime
```sql
-- V2: Health/prefilter state
CREATE TABLE IF NOT EXISTS vanguard_health (
    symbol TEXT NOT NULL,
    cycle_ts_utc TEXT NOT NULL,
    status TEXT,  -- ACTIVE / STALE / HALTED / WARM_UP
    relative_volume REAL,
    spread_bps REAL,
    PRIMARY KEY (symbol, cycle_ts_utc)
);

-- V3: Computed features
CREATE TABLE IF NOT EXISTS vanguard_features (
    symbol TEXT NOT NULL,
    cycle_ts_utc TEXT NOT NULL,
    features_json TEXT,  -- JSON blob of 35 features
    PRIMARY KEY (symbol, cycle_ts_utc)
);

-- V4B: Model predictions
CREATE TABLE IF NOT EXISTS vanguard_predictions (
    symbol TEXT NOT NULL,
    cycle_ts_utc TEXT NOT NULL,
    lgbm_long_prob REAL,
    lgbm_short_prob REAL,
    tcn_long_prob REAL,
    tcn_short_prob REAL,
    PRIMARY KEY (symbol, cycle_ts_utc)
);

-- V5: Strategy shortlists
CREATE TABLE IF NOT EXISTS vanguard_shortlist (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_rank INTEGER,
    strategy_score REAL,
    ml_prob REAL,
    edge_score REAL,
    consensus_count INTEGER DEFAULT 0,
    strategies_matched TEXT,
    regime TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
);

-- V6: Tradeable portfolio per account
CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
    cycle_ts_utc TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL,
    stop_price REAL,
    tp_price REAL,
    shares_or_lots REAL,
    risk_dollars REAL,
    risk_pct REAL,
    position_value REAL,
    intraday_atr REAL,
    edge_score REAL,
    rank INTEGER,
    status TEXT DEFAULT 'APPROVED',
    rejection_reason TEXT,
    PRIMARY KEY (cycle_ts_utc, account_id, symbol)
);

-- V6: Portfolio state per account per day
CREATE TABLE IF NOT EXISTS vanguard_portfolio_state (
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    open_positions INTEGER DEFAULT 0,
    trades_today INTEGER DEFAULT 0,
    daily_realized_pnl REAL DEFAULT 0.0,
    daily_unrealized_pnl REAL DEFAULT 0.0,
    weekly_realized_pnl REAL DEFAULT 0.0,
    total_pnl REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    heat_used_pct REAL DEFAULT 0.0,
    best_day_pnl REAL DEFAULT 0.0,
    todays_volume INTEGER DEFAULT 0,
    todays_fees REAL DEFAULT 0.0,
    daily_pause_level REAL,
    max_loss_level REAL,
    status TEXT DEFAULT 'ACTIVE',
    last_updated_utc TEXT,
    PRIMARY KEY (account_id, date)
);

-- V7: Execution log
CREATE TABLE IF NOT EXISTS vanguard_execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts_utc TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    action TEXT NOT NULL,
    shares_or_lots REAL,
    order_type TEXT,
    limit_price REAL,
    stop_price REAL,
    fill_price REAL,
    fill_quantity REAL,
    execution_bridge TEXT,
    bridge_order_id TEXT,
    status TEXT,
    error_message TEXT,
    latency_ms INTEGER,
    source_system TEXT,
    created_at_utc TEXT DEFAULT (datetime('now'))
);

-- V7: Session log
CREATE TABLE IF NOT EXISTS vanguard_session_log (
    date TEXT PRIMARY KEY,
    session_start_utc TEXT,
    session_end_utc TEXT,
    total_cycles INTEGER DEFAULT 0,
    successful_cycles INTEGER DEFAULT 0,
    failed_cycles INTEGER DEFAULT 0,
    trades_executed INTEGER DEFAULT 0,
    trades_forward_tracked INTEGER DEFAULT 0,
    regime_summary TEXT,
    daily_pnl_by_account TEXT,
    errors TEXT,
    status TEXT DEFAULT 'ACTIVE'
);
```

---

# 3. CONFIG CONTRACT

## Primary Config Files

| File | Purpose |
|---|---|
| `vanguard_accounts.json` | Prop firm accounts (TTP, FTMO) with rules |
| `vanguard_strategies.json` | Strategy registry per asset class |
| `vanguard_orchestrator_config.json` | Session timing, cycle interval |
| `vanguard_execution_config.json` | SignalStack, MT5, Telegram credentials |
| `vanguard_selection_config.json` | ML gate threshold, TBM params |
| `vanguard_instrument_specs.json` | Tick values, lot sizes, pip sizes |
| `vanguard_expected_bars.json` | Per-asset avg exit bars for edge time adjustment |

## Environment Variables (secrets only)
```
ALPACA_KEY
ALPACA_SECRET
IBKR_HOST
IBKR_PORT
MT5_LOGIN
MT5_PASSWORD
MT5_SERVER
SIGNALSTACK_WEBHOOK_URL
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

## Rules
- Config loaded once at startup
- CLI flags may override for testing
- No hardcoded session times, DB paths, URLs, or credentials in stage files
- All thresholds must trace back to config

---

# 4. TIME & TIMESTAMP

## Canonical Rules
- Store all timestamps in DB as **UTC ISO-8601**
- Render all operator-facing times in **America/New_York**
- Market session logic uses **America/New_York**
- Bar slots keyed by bar **end time**

## Session Logic
- First actionable bar (5m): **09:35 ET**
- Last new entry cutoff: **15:30 ET**
- EOD flatten: **15:50 ET**
- Session close: **16:00 ET**

## Forex Sessions (for Session Timing strategy)
- Asian: 19:00-04:00 ET (Sun-Fri)
- London: 03:00-12:00 ET
- NY: 08:00-17:00 ET
- London/NY overlap: 08:00-12:00 ET (peak liquidity)

## Holidays
- Use `exchange_calendars` or `pandas_market_calendars` library
- No hand-coded holiday lists

---

# 5. SHARED HELPERS & DATA ADAPTERS

## Helper Modules

| Module | Purpose |
|---|---|
| `clock.py` | UTC/ET time, market open/close, bar slot rounding |
| `market_sessions.py` | Session boundaries, holidays, half-day checks |
| `db.py` | SQLite WAL connection, upsert wrappers, batch writes |
| `bars.py` | Bar normalization, 1m→5m→1h aggregation |
| `quality.py` | NaN checks, outlier detection, min bar counts |
| `normalize.py` | Feature normalization (percentile, z-score, min-max) |
| `regime_detector.py` | 5-feature regime gate (ACTIVE/CAUTION/DEAD/CLOSED) |
| `strategy_router.py` | Maps asset_class → list of strategy scoring functions |
| `consensus_counter.py` | Counts multi-strategy agreement per instrument |
| `position_sizer.py` | Shares (equities), lots (forex), contracts (futures) |
| `portfolio_state.py` | Per-account state: P&L, positions, budgets |
| `correlation_checker.py` | Pairwise correlation between positions |
| `eod_flatten.py` | EOD close logic for day-trade accounts |
| `ttp_rules.py` | TTP-specific: daily pause, scaling, consistency |

## Data Adapters

| Adapter | Data Source | Usage |
|---|---|---|
| `alpaca_adapter.py` | Alpaca IEX (free) | US equities bars (Phase 1) |
| `ibkr_adapter.py` | IBKR TWS API via `ib_async` | US equities SIP + forex + futures (Phase 5) |
| `mt5_data_adapter.py` | MetaTrader5 Python | FTMO CFDs: metals, agriculture, equity CFDs (Phase 5) |

## Execution Adapters

| Adapter | Target | Usage |
|---|---|---|
| `signalstack_adapter.py` | SignalStack webhook → TTP | US equities execution |
| `mt5_adapter.py` | MT5 terminal → FTMO | CFD/forex execution |
| `telegram_alerts.py` | Telegram bot API | Alerts, reports, errors |

## Allowed Imports from Other Systems
- Generic SQLite helpers (patterns only, not imports)
- Generic logging patterns
- Generic config-loading patterns
- Alpaca auth credentials (shared env vars)

## Forbidden Imports
- No import from `stages/v2_*.py` (Meridian)
- No import from S1 stage files
- No import from daily factor engine internals
- No import from daily selection logic
- No import from daily DB path constants

---

# 6. CANONICAL DOC SET — Read Order

## Core Stage Specs (V1-V7)
1. `VANGUARD_STAGE_V1_SPEC.md`
2. `VANGUARD_STAGE_V2_SPEC.md`
3. `VANGUARD_STAGE_V3_SPEC.md`
4. `VANGUARD_STAGE_V4A_SPEC.md`
5. `VANGUARD_STAGE_V4B_SPEC.md`
6. `VANGUARD_STAGE_V5_SPEC.md` (v2 — Strategy Router)
7. `VANGUARD_STAGE_V6_SPEC.md` (TTP platform data)
8. `VANGUARD_STAGE_V7_SPEC.md` (Orchestrator + Execution Bridge)

## Supporting Specs
9. `VANGUARD_SUPPORTING_SPECS.md` (this file — repo, DB, config, time, helpers)
10. `VANGUARD_BUILD_PLAN.md`

## Reference Data
11. `PROJECT_VANGUARD_HANDOFF.md`
12. `ftmo_universe_complete.json`
13. `ftmo_universe_with_futures.json`

## Build Agent Read Order
1. This file (canonical doc set)
2. Supporting specs (repo structure, DB, config, time, helpers)
3. Stage V7 + execution adapters (Phase 0 — build first)
4. Stage V1-V2 (Phase 1)
5. Stage V3-V4B (Phase 2)
6. Stage V5-V6 (Phase 3)
7. Build plan for timeline

## Global Rules
- Vanguard is PARALLEL to Meridian and S1 — never mutates their code
- DB isolation enforced at startup
- UTC storage, ET display
- Execution defaults to OFF (forward tracking mode)
- The executor bridge is SYSTEM-AGNOSTIC — serves Meridian, S1, and Vanguard
- The React UI at signalstack-app.vercel.app is the frontend — backend plugs in via API
- All timestamps UTC in storage, ET in display
- No hardcoded paths, URLs, or credentials in stage files

## If Specs Conflict
- Stage spec wins over supporting spec for stage-specific behavior
- Supporting spec wins for cross-cutting concerns (DB isolation, timestamps, config)
- This canonical doc set is the authority for read order and precedence
