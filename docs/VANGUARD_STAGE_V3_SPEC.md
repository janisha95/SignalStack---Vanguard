# STAGE V3: Vanguard Factor Engine — vanguard_factor_engine.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 28, 2026
**Depends on:** V1 (bars in DB) + V2 (FTMO survivors) + V1 equity survivors

---

## What Stage V3 Does

Computes 35 features for all survivors (~1,650-2,180 instruments) every 5
minutes during active sessions. Features are designed to work across ALL
asset classes (equities, forex, indices, metals, agriculture, energy, crypto,
futures) and feed into a 3-model ensemble (NN side-aware + LGBM short
specialist + RF/LGBM long specialist).

V3 is a FACTOR STAGE ONLY. No scoring, no ranking, no direction decisions.
It produces a feature matrix consumed by V4B (model trainer) and V5 (selection).

---

## 35 Features Across 10 Groups

### Group 1: Price Location (4 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 1 | session_vwap_distance | (close - VWAP) / VWAP | VWAP uses tick_volume for forex/CFDs, real volume for equities/futures. Session-aware: resets at session open per asset class. |
| 2 | session_opening_range_position | (close - OR_low) / (OR_high - OR_low) | OR = first 3 5m bars of session. Session open defined per asset class: equities=9:30 ET, forex=NY 8:00 AM, crypto=00:00 UTC. |
| 3 | gap_pct | (session_open - prev_session_close) / prev_session_close | 0 for 24/7 crypto. Uses previous session close, not previous bar close. |
| 4 | dist_from_session_high_low | (close - session_low) / (session_high - session_low) | 1.0 = at session high, 0.0 = at session low. |

### Group 2: Momentum (4 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 5 | momentum_3bar | (close - close_3bars_ago) / close_3bars_ago | 15-minute momentum on 5m bars. |
| 6 | momentum_12bar | (close - close_12bars_ago) / close_12bars_ago | 60-minute momentum on 5m bars. |
| 7 | momentum_acceleration | momentum_3bar - momentum_3bar_3bars_ago | Is momentum speeding up or slowing down? |
| 8 | atr_expansion | ATR(5m, 5-bar) / ATR(5m, 20-bar avg) | >1.0 = volatility expanding, <1.0 = contracting. Universal vol regime signal. |

### Group 3: Volume (4 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 9 | relative_volume | current_bar_volume / session_avg_volume | Uses tick_volume for forex/CFDs, real volume for equities/futures. |
| 10 | volume_burst_z | (current_vol - mean_20bar) / std_20bar | Z-score of current bar volume. |
| 11 | effort_vs_result | abs(close - open) / (volume + 1) | High volume + small move = absorption. Low volume + big move = no conviction. Normalized per asset class. |
| 12 | down_volume_ratio | sum(volume where close < open, last 12 bars) / sum(total volume, last 12 bars) | Direct selling pressure measurement. >0.5 = sellers dominating. SHORT signal. |

### Group 4: Market Context (3 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 13 | rs_vs_benchmark_intraday | instrument_return_12bar - benchmark_return_12bar | Benchmark per asset class: SPY for equities, DXY for forex, BTCUSD for crypto alts, US500 for indices, XAUUSD for metals. |
| 14 | benchmark_momentum_12bar | benchmark 60-min return | Same per-asset benchmark. |
| 15 | cross_asset_correlation | rolling_corr(instrument, asset_class_peers, 20 bars) | How correlated is this instrument to its peers right now? High = herding/risk-off. |

### Group 5: Daily Bridge (5 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 16 | daily_atr_pct | ATR(14, daily) / close | Daily volatility context. Computed from daily bars. |
| 17 | daily_conviction | +1 if daily long pick, -1 if daily short pick, 0 otherwise | Set to 0 for ALL instruments in v1. Daily system integration added later. Signed for short-side support. |
| 18 | daily_rs_vs_benchmark | 20-day return vs benchmark 20-day return | Per-asset benchmark. |
| 19 | daily_drawdown_from_high | (close - max(close, 20 days)) / max(close, 20 days) | Negative value = how far below recent high. Large negative = damaged. SHORT signal. |
| 20 | daily_adx | Wilder's ADX(14) on daily bars | 0-100. High = trending, low = choppy. Universal. |

### Group 6: SMC — 5-Minute (6 features)

Computed using `smartmoneyconcepts` Python library on 5m bars.

| # | Feature | Computation | Notes |
|---|---|---|---|
| 21 | fvg_bullish_nearest | Distance to nearest unfilled bullish FVG / ATR | Normalized by ATR. 0 = price is AT the FVG. Large = far away. |
| 22 | fvg_bearish_nearest | Distance to nearest unfilled bearish FVG / ATR | Same normalization. |
| 23 | ob_proximity_5m | Signed distance to nearest OB / ATR | Positive = near bullish OB (demand), Negative = near bearish OB (supply). |
| 24 | structure_break | +1 if bullish BOS, -1 if bearish BOS, 0 if none | Break of structure in last 3 bars. |
| 25 | liquidity_sweep | +1 if swept lows then reclaimed, -1 if swept highs then rejected, 0 | Wick beyond swing point then close back. |
| 26 | premium_discount_zone | (close - swing_low) / (swing_high - swing_low) | >0.5 = premium (sell zone), <0.5 = discount (buy zone). |

### Group 7: SMC — Higher Timeframe 1H (4 features)

Computed using `smartmoneyconcepts` on 1H bars from vanguard_bars_1h.

| # | Feature | Computation | Notes |
|---|---|---|---|
| 27 | htf_trend_direction | +1 bullish, -1 bearish, 0 neutral | Based on 1H swing structure (higher highs/lows vs lower). |
| 28 | htf_structure_break | +1 bullish BOS, -1 bearish BOS, 0 none | 1H timeframe BOS/CHoCH. |
| 29 | htf_fvg_nearest | Signed distance to nearest 1H FVG / daily ATR | Positive = bullish FVG below (support), Negative = bearish FVG above (resistance). |
| 30 | htf_ob_proximity | Signed distance to nearest 1H OB / daily ATR | Same signed convention as ob_proximity_5m. |

### Group 8: Session / Time (3 features)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 31 | session_phase | Encoded integer | Forex: Asia=0, London=1, NY=2, overlap=3. Equities: pre=0, open=1, mid=2, close=3. |
| 32 | time_in_session_pct | minutes_since_open / session_duration | 0.0 = just opened, 1.0 = about to close. |
| 33 | bars_since_session_open | Count of 5m bars since session open | Raw count. Model learns warm-up effects. |

### Group 9: Quality (1 feature)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 34 | spread_proxy | current_spread / avg_spread_100bar | MT5 instruments: from spread field. Alpaca/IBKR: estimated from bid-ask or high-low. >1.0 = wider than normal. |

### Group 10: Meta (1 feature)

| # | Feature | Computation | Notes |
|---|---|---|---|
| 35 | asset_class | Encoded integer | forex=0, index=1, metal=2, agriculture=3, energy=4, equity=5, crypto=6, futures=7. |

---

## 3-Model Feature Allocation

V3 computes ALL 35 features for every survivor. Feature allocation to
models is V4B's responsibility. For reference:

| Model | Purpose | Features | Count |
|---|---|---|---|
| NN (side-aware) | Generalist, 3-class TBM | All 35 | 35 |
| LGBM Short | Short specialist, inverted TBM | Short-biased subset | 22 |
| RF/LGBM Long | Long specialist, standard TBM | Long-biased subset | 22 |

Feature subsets defined in config/vanguard_model_features.json (V4B spec).

---

## Per-Asset-Class Benchmark Map

| Asset Class | Benchmark Symbol | Source |
|---|---|---|
| equity | SPY | Alpaca / IBKR |
| index | US500.cash (or SPY) | MT5 / IBKR |
| forex | DXY.cash | MT5 / IBKR |
| metal | XAUUSD | MT5 / IBKR |
| agriculture | (none — use own ATR context) | — |
| energy | USOIL.cash (or CL) | MT5 / IBKR |
| crypto | BTCUSD | MT5 / IBKR |
| futures | Matching cash index | IBKR |

Benchmarks MUST be in the V1 streaming universe. If benchmark is missing,
rs_vs_benchmark features default to 0.0.

---

## Input Contract

| Input | Source | Notes |
|---|---|---|
| Equity survivors | V1 prefilter | ~1,500-2,000 equities |
| FTMO survivors | V2 health monitor | ~150-180 instruments |
| vanguard_bars_5m | V1 DB | Primary computation timeframe |
| vanguard_bars_1h | V1 DB | For HTF SMC features |
| Daily bars | V1 DB or daily cache | For daily bridge features |
| Benchmark bars | V1 DB | SPY, DXY, BTCUSD, etc. |
| config/vanguard_universe.json | Config | Asset class + session definitions |

---

## Output Contract

### Table: vanguard_factor_matrix

```sql
CREATE TABLE IF NOT EXISTS vanguard_factor_matrix (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    path TEXT NOT NULL,
    -- Price Location
    session_vwap_distance REAL,
    session_opening_range_position REAL,
    gap_pct REAL,
    dist_from_session_high_low REAL,
    -- Momentum
    momentum_3bar REAL,
    momentum_12bar REAL,
    momentum_acceleration REAL,
    atr_expansion REAL,
    -- Volume
    relative_volume REAL,
    volume_burst_z REAL,
    effort_vs_result REAL,
    down_volume_ratio REAL,
    -- Market Context
    rs_vs_benchmark_intraday REAL,
    benchmark_momentum_12bar REAL,
    cross_asset_correlation REAL,
    -- Daily Bridge
    daily_atr_pct REAL,
    daily_conviction REAL,
    daily_rs_vs_benchmark REAL,
    daily_drawdown_from_high REAL,
    daily_adx REAL,
    -- SMC 5m
    fvg_bullish_nearest REAL,
    fvg_bearish_nearest REAL,
    ob_proximity_5m REAL,
    structure_break REAL,
    liquidity_sweep REAL,
    premium_discount_zone REAL,
    -- SMC HTF 1H
    htf_trend_direction REAL,
    htf_structure_break REAL,
    htf_fvg_nearest REAL,
    htf_ob_proximity REAL,
    -- Session/Time
    session_phase REAL,
    time_in_session_pct REAL,
    bars_since_session_open REAL,
    -- Quality
    spread_proxy REAL,
    -- Meta
    asset_class_encoded REAL,
    -- Metadata
    compute_time_ms INTEGER,
    features_computed INTEGER,
    PRIMARY KEY (cycle_ts_utc, symbol)
);
CREATE INDEX IF NOT EXISTS idx_vg_fm_cycle ON vanguard_factor_matrix(cycle_ts_utc);
CREATE INDEX IF NOT EXISTS idx_vg_fm_path ON vanguard_factor_matrix(path, cycle_ts_utc);
```

---

## Computation Architecture

### Module Split (by data source, not by theme)

```
vanguard_factor_engine.py (orchestrator)
├── modules/
│   ├── m1_price_momentum.py    # Features 1-8: price location + momentum
│   │                           # Source: 5m bars only
│   │
│   ├── m2_volume.py            # Features 9-12: volume features
│   │                           # Source: 5m bars only
│   │
│   ├── m3_context.py           # Features 13-15: market context
│   │                           # Source: 5m bars + benchmark bars
│   │
│   ├── m4_daily_bridge.py      # Features 16-20: daily context
│   │                           # Source: daily bars (computed ONCE per day, cached)
│   │
│   ├── m5_smc_5m.py            # Features 21-26: SMC on 5m bars
│   │                           # Source: 5m bars, uses smartmoneyconcepts lib
│   │
│   ├── m6_smc_htf.py           # Features 27-30: SMC on 1H bars
│   │                           # Source: 1h bars, uses smartmoneyconcepts lib
│   │
│   └── m7_session_meta.py      # Features 31-35: session/time/quality/meta
│                               # Source: config + current time + spread data
```

### Why This Split

- m1 + m2 are the fastest (pure OHLCV math on 5m bars)
- m3 needs benchmark bars (separate query)
- m4 is computed ONCE per day and cached (expensive daily bar queries)
- m5 + m6 are the heaviest (SMC library calls). m5 runs on 5m, m6 on 1H
- m7 is trivial (lookups and time math)

### Execution Order

```python
def compute_factors(db, survivors, cycle_ts):
    results = {}

    # Fast modules first (can run in parallel if needed)
    bars_5m = load_5m_bars(db, survivors, lookback=25)  # 25 bars = 2+ hours
    bars_1h = load_1h_bars(db, survivors, lookback=24)  # 24 bars = 1 day

    r1 = m1_price_momentum.compute(bars_5m, survivors)
    r2 = m2_volume.compute(bars_5m, survivors)
    r3 = m3_context.compute(bars_5m, survivors, benchmarks)
    r7 = m7_session_meta.compute(survivors, cycle_ts, config)

    # Daily bridge — cached, only recomputed once per day
    r4 = m4_daily_bridge.compute_or_cache(db, survivors)

    # SMC modules — heaviest computation
    r5 = m5_smc_5m.compute(bars_5m, survivors)
    r6 = m6_smc_htf.compute(bars_1h, survivors)

    # Merge all into factor matrix
    matrix = merge_modules(r1, r2, r3, r4, r5, r6, r7)

    # Write to DB
    write_factor_matrix(db, matrix, cycle_ts)

    return matrix
```

---

## SMC Computation Details

### smartmoneyconcepts Library

```python
from smartmoneyconcepts import smc

# For each symbol's 5m OHLCV DataFrame:
fvg = smc.fvg(ohlc, join_consecutive=True)
ob = smc.ob(ohlc, method='atr')  # ATR-based order blocks
bos = smc.bos(ohlc)              # Break of structure
liquidity = smc.liquidity(ohlc)
```

### Lookback Requirements

| Feature Group | Minimum 5m bars | Minimum 1H bars | Notes |
|---|---|---|---|
| Price Location | 15 (OR needs 3) | — | 75 min of data |
| Momentum | 15 | — | 12-bar momentum + 3-bar lag |
| Volume | 25 | — | 20-bar average + current |
| Market Context | 25 | — | 20-bar rolling correlation |
| Daily Bridge | — | — | Daily bars (separate query) |
| SMC 5m | 50 | — | Need swing structure to form |
| SMC HTF 1H | — | 24 | 1 day of 1H bars minimum |
| Session/Time | 1 | — | Current bar + config lookup |

**Minimum warm-up: 50 5m bars (4+ hours) for full SMC computation.**
Before that, SMC features default to 0.0. Other features available after 15 bars.

---

## Volume Handling: Tick vs Real

| Asset Class | Volume Source | Field Used |
|---|---|---|
| equity (Alpaca/IBKR) | Real exchange volume | volume |
| futures (IBKR) | Real exchange volume | volume |
| forex (MT5/IBKR) | Tick volume | tick_volume |
| index CFD (MT5) | Tick volume | tick_volume |
| metal CFD (MT5) | Tick volume | tick_volume |
| agriculture CFD (MT5) | Tick volume | tick_volume |
| energy CFD (MT5) | Tick volume | tick_volume |
| equity CFD (MT5) | Tick volume | tick_volume |
| crypto (MT5) | Tick volume | tick_volume |

V3 checks asset_class to determine which volume field to use.

---

## Performance Targets

| Metric | Target | Red Flag |
|---|---|---|
| Total compute time (all modules) | < 90 sec | > 120 sec |
| m1 + m2 (price/momentum/volume) | < 15 sec | > 30 sec |
| m3 (market context) | < 10 sec | > 20 sec |
| m4 (daily bridge, cached) | < 5 sec | > 15 sec |
| m5 (SMC 5m) | < 30 sec | > 45 sec |
| m6 (SMC HTF 1H) | < 15 sec | > 25 sec |
| m7 (session/meta) | < 2 sec | > 5 sec |
| Survivors processed | ~1,650-2,180 | < 500 (something wrong) |
| Features per survivor | 35 | < 30 (module failure) |

---

## Failure Handling

### Partial Failure (module fails, rest succeed)

If one module fails (e.g., m5_smc_5m errors for some symbols):
- Log the failure with affected symbols
- Set affected features to NaN for those symbols
- Still write the matrix row with available features
- Mark row: features_computed < 35
- Downstream models handle NaN via imputation

### Hard Fail

- Zero survivors passed in → abort cycle
- DB unavailable → abort cycle
- All modules fail → abort cycle, Telegram alert

### Soft Warning

- > 10% of features are NaN for any symbol → warn
- Compute time exceeds 90 sec → warn
- Benchmark symbol missing → default rs features to 0.0, warn

---

## Logging

```text
[V3] Factor cycle at 2026-03-28T14:05:00Z | survivors=1,847
[V3] m1_price_momentum: 1,847 symbols, 8.2s
[V3] m2_volume: 1,847 symbols, 4.1s
[V3] m3_context: 1,847 symbols, 7.3s
[V3] m4_daily_bridge: cached (computed at 09:35)
[V3] m5_smc_5m: 1,847 symbols, 24.6s
[V3] m6_smc_htf: 1,847 symbols, 12.1s
[V3] m7_session_meta: 1,847 symbols, 0.8s
[V3] Total: 35 features × 1,847 symbols in 57.1s | NaN rate: 2.1%
```

---

## File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_factor_engine.py       # V3 orchestrator
├── vanguard/
│   ├── modules/
│   │   ├── m1_price_momentum.py
│   │   ├── m2_volume.py
│   │   ├── m3_context.py
│   │   ├── m4_daily_bridge.py
│   │   ├── m5_smc_5m.py
│   │   ├── m6_smc_htf.py
│   │   └── m7_session_meta.py
│   ├── helpers/
│   │   ├── session_manager.py          # Shared
│   │   ├── db.py                       # Shared
│   │   └── clock.py                    # Shared
│   └── __init__.py
├── config/
│   ├── vanguard_benchmarks.json        # Per-asset-class benchmark mapping
│   ├── vanguard_factor_registry.json   # 35 features with metadata
│   └── vanguard_session_definitions.json
└── tests/
    ├── test_vanguard_factors.py
    ├── test_m5_smc.py
    └── test_m6_htf.py
```

---

## CLI

```bash
# Normal — called by orchestrator every 5 min
python3 stages/vanguard_factor_engine.py

# Dry run — compute for 5 symbols, print matrix, no DB write
python3 stages/vanguard_factor_engine.py --dry-run --symbols AAPL,EURUSD,XAUUSD,BTCUSD,ES

# Single module test
python3 stages/vanguard_factor_engine.py --module m5_smc_5m --symbols EURUSD

# Force daily bridge recompute
python3 stages/vanguard_factor_engine.py --recompute-daily
```

---

## Acceptance Criteria

- [ ] stages/vanguard_factor_engine.py exists and runs
- [ ] 35 features computed for all survivors
- [ ] Multi-asset: works on equities, forex, indices, metals, agriculture, energy, crypto, futures
- [ ] Per-asset-class benchmarks (SPY, DXY, BTCUSD, etc.)
- [ ] Tick volume used for forex/CFDs, real volume for equities/futures
- [ ] SMC features via smartmoneyconcepts library on 5m bars
- [ ] HTF SMC features on 1H bars
- [ ] Session-aware VWAP, opening range, session phase
- [ ] Daily bridge features computed once per day, cached
- [ ] Signed ob_proximity (positive=bullish, negative=bearish)
- [ ] down_volume_ratio feature for short-side signal
- [ ] daily_drawdown_from_high for short-side damage detection
- [ ] daily_conviction = 0 for all (daily system integration later)
- [ ] Writes vanguard_factor_matrix to DB
- [ ] Handles partial module failures gracefully (NaN + log)
- [ ] Total compute < 90 sec for ~2,000 survivors
- [ ] No ranking, no scoring, no direction decisions
- [ ] No imports from v2_*.py (Meridian)

---

## Out of Scope

- Model training (V4B)
- Model scoring / prediction (V4B/V5)
- Feature selection per model (V4B config)
- Selection / ranking (V5)
- Risk filters (V6)
- Execution (V7)
- Agreement logic between models (V5)
- TBM labeling (V4A)
