# STAGE V4A: Vanguard Training Backfill — vanguard_training_backfill.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 28, 2026
**Depends on:** V1 (historical bars) + V3 (factor engine for historical replay)

---

## What Stage V4A Does

Builds the intraday model training dataset by:
1. Replaying V3 factor computation on historical 5m bars
2. Generating TBM labels for BOTH long and short directions
3. Enforcing session-aware label paths (no cross-session leakage)
4. Writing a single training table consumed by V4B (model trainer)

Designed for fast chunked backfill — process by date, not by ticker.

---

## Two Label Sets (Same Table, Two Columns)

For each row at time T with entry = close at T:

### label_long (Standard TBM)
- Walk forward bars (T+1 ... T+horizon) within same session
- label = 1 if HIGH reaches entry × (1 + tp_pct) before LOW reaches entry × (1 - sl_pct)
- label = 0 if SL hit first OR timeout

### label_short (Inverted TBM)
- Walk forward bars (T+1 ... T+horizon) within same session
- label = 1 if LOW reaches entry × (1 - tp_pct) before HIGH reaches entry × (1 + sl_pct)
- label = 0 if SL hit first (price rises) OR timeout

Both labels computed from the SAME forward path scan — one pass per row.

---

## Default TBM Parameters

| Parameter | Default | Configurable | Notes |
|---|---|---|---|
| bar_interval | 5m | Yes | Primary modeling timeframe |
| horizon_bars | 6 | Yes | 30 minutes on 5m bars |
| tp_pct | 0.0030 | Yes | +0.30% take profit |
| sl_pct | 0.0015 | Yes | -0.15% stop loss |
| breakeven_wr | 0.333 | — | 33.3% at 2:1 R/R |

May need per-asset-class tuning later (forex moves less than small-cap equities
in 30 minutes). Start with universal defaults, tune after first training.

---

## Session Boundary Rules (MANDATORY)

### No cross-session leakage

| Asset Class | Session End | Rule |
|---|---|---|
| US Equities | 4:00 PM ET | No path past 4:00 PM |
| Forex | Daily rollover ~5 PM ET | No path across rollover |
| EU Equity CFDs | 11:30 AM ET | No path past EU close |
| Crypto | UTC day boundary 00:00 | No path across midnight UTC |
| Metals/Energy | 5:00 PM ET daily break | No path across break |
| Futures | 5:00 PM ET daily break | Same |

### Late-session handling
If fewer than horizon_bars remain before session end:
- Timeout at session close
- Label based on actual return at close vs entry
- Mark row: truncated = True

### Warm-up flagging
Rows where bars_since_session_open < 15 (75 min):
- Flag: warm_up = True
- Still include in training table
- V4B decides whether to use them

---

## Pipeline

```
For each historical date:
    For each session in that date:
        Load 5m bars for all symbols active in that session
        Replay V3 factor computation → factor_matrix snapshot at each 5m bar
        For each (symbol, timestamp) in factor_matrix:
            Load forward 5m bars (T+1 ... T+horizon) within session
            One-pass forward scan:
                Track cumulative high and low
                Check long TP/SL barriers
                Check short TP/SL barriers (inverted)
                Record exit_bar, forward_return, MFE, MAE
            Write row to vanguard_training_data
```

---

## Output Contract

### Table: vanguard_training_data

```sql
CREATE TABLE IF NOT EXISTS vanguard_training_data (
    asof_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    path TEXT NOT NULL,
    -- Entry
    entry_price REAL NOT NULL,
    -- Labels
    label_long INTEGER NOT NULL,        -- 1 = long TP hit, 0 = SL/timeout
    label_short INTEGER NOT NULL,       -- 1 = short TP hit, 0 = SL/timeout
    -- Auxiliary targets
    forward_return REAL,                -- actual return at exit
    max_favorable_excursion REAL,       -- max profit during path
    max_adverse_excursion REAL,         -- max drawdown during path
    exit_bar INTEGER,                   -- which bar (1-6) barrier was hit
    exit_type_long TEXT,                -- TP / SL / TIMEOUT
    exit_type_short TEXT,               -- TP / SL / TIMEOUT
    truncated INTEGER DEFAULT 0,        -- 1 if session end forced early exit
    warm_up INTEGER DEFAULT 0,          -- 1 if < 15 bars since session open
    -- All 35 features (copied from factor_matrix at time T)
    session_vwap_distance REAL,
    session_opening_range_position REAL,
    gap_pct REAL,
    dist_from_session_high_low REAL,
    momentum_3bar REAL,
    momentum_12bar REAL,
    momentum_acceleration REAL,
    atr_expansion REAL,
    relative_volume REAL,
    volume_burst_z REAL,
    effort_vs_result REAL,
    down_volume_ratio REAL,
    rs_vs_benchmark_intraday REAL,
    benchmark_momentum_12bar REAL,
    cross_asset_correlation REAL,
    daily_atr_pct REAL,
    daily_conviction REAL,
    daily_rs_vs_benchmark REAL,
    daily_drawdown_from_high REAL,
    daily_adx REAL,
    fvg_bullish_nearest REAL,
    fvg_bearish_nearest REAL,
    ob_proximity_5m REAL,
    structure_break REAL,
    liquidity_sweep REAL,
    premium_discount_zone REAL,
    htf_trend_direction REAL,
    htf_structure_break REAL,
    htf_fvg_nearest REAL,
    htf_ob_proximity REAL,
    session_phase REAL,
    time_in_session_pct REAL,
    bars_since_session_open REAL,
    spread_proxy REAL,
    asset_class_encoded REAL,
    -- TBM config used
    horizon_bars INTEGER NOT NULL,
    tp_pct REAL NOT NULL,
    sl_pct REAL NOT NULL,
    PRIMARY KEY (asof_ts_utc, symbol, horizon_bars)
);
CREATE INDEX IF NOT EXISTS idx_vg_train_asset ON vanguard_training_data(asset_class);
CREATE INDEX IF NOT EXISTS idx_vg_train_date ON vanguard_training_data(asof_ts_utc);
CREATE INDEX IF NOT EXISTS idx_vg_train_long ON vanguard_training_data(label_long);
CREATE INDEX IF NOT EXISTS idx_vg_train_short ON vanguard_training_data(label_short);
```

---

## Data Sources for Historical Bars

| Source | Coverage | Method |
|---|---|---|
| Alpaca REST (now) | US equities, IEX 1m bars | Free, limited history |
| IBKR (Monday) | US equities full SIP + forex + futures | reqHistoricalData |
| MT5 | FTMO CFDs | copy_rates_range, 2-6 months typically |
| Databento | Deep history if needed | One-time purchase |

Start with whatever V1 has collected. Backfill deeper as data accumulates.
3-6 months of 5m bars is sufficient for initial LightGBM training.
TCN needs more (6-12 months for 64-bar sequences).

---

## Incremental Backfill

V4A MUST support resume-safe incremental append:
- Detect which (asof_ts_utc, symbol, horizon_bars) already exist
- Skip existing rows in incremental mode
- --full-rebuild overwrites everything
- Idempotent on re-run

---

## CLI

```bash
# Full rebuild
python3 stages/vanguard_training_backfill.py --full-rebuild

# Incremental (append new dates only)
python3 stages/vanguard_training_backfill.py

# Date range
python3 stages/vanguard_training_backfill.py --start-date 2026-01-01 --end-date 2026-03-28

# Specific asset class
python3 stages/vanguard_training_backfill.py --asset-class equity

# Custom TBM params
python3 stages/vanguard_training_backfill.py --tp-pct 0.005 --sl-pct 0.0025 --horizon-bars 12

# Dry run
python3 stages/vanguard_training_backfill.py --dry-run
```

---

## Performance Targets

| Metric | Target | Notes |
|---|---|---|
| Rows per session (equities) | ~1,500 × 78 bars = ~117K | Per trading day |
| Rows per session (FTMO) | ~180 × varies = ~20-50K | Depends on session hours |
| Backfill speed | > 10K rows/minute | Chunked by date |
| DB size per month | ~500MB - 1GB | 35 features × ~150K rows/day |

---

## Failure Handling

### Hard Fail
- No historical bars in DB
- Factor replay produces zero rows
- Cross-session leakage detected (validate paths)

### Soft Warning
- Sparse factors for early warm-up rows → flag, include
- Some symbols skipped (halts, gaps) → log, continue
- Late-session truncated labels → flag, include

---

## Acceptance Criteria

- [ ] stages/vanguard_training_backfill.py exists
- [ ] Writes vanguard_training_data with both label_long and label_short
- [ ] TBM params configurable via CLI
- [ ] Session-aware: no cross-session leakage
- [ ] Per-asset-class session boundaries
- [ ] Incremental append supported and idempotent
- [ ] Full rebuild supported
- [ ] warm_up and truncated flags set correctly
- [ ] All 35 features copied from factor matrix
- [ ] MFE, MAE, exit_bar, forward_return computed
- [ ] Chunked by date for performance
- [ ] No imports from v2_*.py (Meridian)
