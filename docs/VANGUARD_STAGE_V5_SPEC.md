# STAGE V5: Vanguard Selection — Strategy Router — vanguard_selection.py

**Status:** APPROVED (v2 — Strategy Router)
**Project:** Vanguard
**Date:** Mar 29, 2026
**Depends on:** V4B (model predictions) + V3 (features)

---

## What Stage V5 Does

Three jobs:

1. **Regime Gate** — should we trade this cycle? (binary on/off)
2. **Strategy Router** — run asset-class-specific strategy scoring functions
   to produce MULTIPLE ranked lists from different "lenses"
3. **Consensus Count** — how many strategies agree on each pick?

The ML models already encode all 35 features. The strategies don't
replace ML — they apply different RANKING FUNCTIONS to the same
model outputs + features, producing multiple views of the universe.

---

## Architecture

```
V4B model predictions + V3 features for all survivors
        │
        ├── Regime Gate (trade or skip this cycle)
        │
        ├── EQUITIES (TTP instruments)
        │   ├── SMC Confluence Strategy → top 5 LONG + 5 SHORT
        │   ├── Momentum Strategy → top 5+5
        │   ├── Mean Reversion Strategy → top 5+5
        │   ├── Risk-Reward Edge Strategy → top 5+5
        │   ├── Relative Strength Strategy → top 5+5
        │   └── Breakdown / Damage Strategy → SHORT ONLY top 5 (from S1 BRF)
        │
        ├── FOREX (FTMO pairs)
        │   ├── SMC Confluence Strategy → top 5+5
        │   ├── Session Timing Strategy → top 3+3
        │   ├── HTF Alignment Strategy → top 3+3
        │   ├── Liquidity Grab Reversal Strategy → top 3+3
        │   └── Risk-Reward Edge Strategy → top 5+5
        │
        ├── INDICES (FTMO indices)
        │   ├── Momentum Strategy → top 3+3
        │   ├── Session Open Strategy → top 3+3
        │   └── Risk-Reward Edge Strategy → top 3+3
        │
        ├── METALS (FTMO metals)
        │   ├── SMC Confluence Strategy → top 3+3
        │   ├── Cross-Asset Strategy → top 3+3
        │   └── Risk-Reward Edge Strategy → top 3+3
        │
        ├── CRYPTO (FTMO crypto)
        │   ├── SMC Confluence Strategy → top 3+3
        │   ├── Momentum Breakout Strategy → top 3+3
        │   └── Risk-Reward Edge Strategy → top 3+3
        │
        ├── ENERGY/AGRICULTURE (FTMO commodities)
        │   ├── Momentum Strategy → top 3+3
        │   └── Risk-Reward Edge Strategy → top 3+3
        │
        └── LLM Strategy (optional, per asset class, default OFF)
            └── Qualitative overlay using regime + news + sentiment
```

---

## Part 1: Regime Gate

Same as before. 5 features from V3 determine if we trade this cycle.

| Regime | Detection | Action |
|---|---|---|
| ACTIVE | volume > 0.5 AND not dead zone | Trade normally |
| CAUTION | atr_expansion > 2.0 | Raise edge threshold |
| DEAD | lunch hour AND low volume | Skip cycle |
| CLOSED | Outside session hours | Skip cycle |

---

## Part 2: Strategy Definitions

### ML Gate (applies to ALL strategies)
Every candidate must pass `ml_prob > 0.50` before ANY strategy scores it.
This is the universal quality floor.

### EQUITIES Strategies

**1. SMC Confluence**
```python
smc_score = (
    w1 * normalize(ob_proximity_5m) +      # near unmitigated order block
    w2 * normalize(fvg_bullish_nearest) +   # near unfilled FVG
    w3 * normalize(structure_break) +        # recent BOS/CHoCH
    w4 * normalize(liquidity_sweep) +        # recent stop hunt → reversal
    w5 * normalize(premium_discount_zone) +  # correct zone for direction
    w6 * normalize(htf_trend_direction)      # HTF alignment
)
# Default weights: w1-w6 = equal (1/6 each)
# top 5 LONG + 5 SHORT by smc_score
```

**2. Momentum**
```python
momentum_score = (
    w1 * normalize(momentum_3bar) +
    w2 * normalize(momentum_12bar) +
    w3 * normalize(momentum_acceleration) +
    w4 * normalize(atr_expansion) +
    w5 * normalize(volume_burst_z)
)
# top 5 LONG + 5 SHORT by momentum_score
```

**3. Mean Reversion**
```python
reversion_score = (
    w1 * normalize(abs(session_vwap_distance)) +  # far from VWAP
    w2 * normalize(extreme_premium_discount) +     # at zone extremes
    w3 * normalize(daily_drawdown_from_high) +     # extended from high
    w4 * normalize(effort_vs_result)               # volume disagrees with price
)
# LONG: deep discount + far below VWAP + volume divergence
# SHORT: deep premium + far above VWAP + volume divergence
# top 5 LONG + 5 SHORT by reversion_score
```

**4. Risk-Reward Edge**
```python
edge = (ml_prob * tp_pct) - ((1 - ml_prob) * sl_pct) - spread_cost
time_adjusted_edge = edge / sqrt(expected_bars)
# top 5 LONG + 5 SHORT by time_adjusted_edge
```

**5. Relative Strength**
```python
rs_score = (
    w1 * normalize(rs_vs_benchmark_intraday) +
    w2 * normalize(daily_rs_vs_benchmark) +
    w3 * normalize(benchmark_momentum_12bar)
)
# LONG: strong RS in bullish benchmark → riding the leader
# SHORT: weak RS in bearish benchmark → shorting the laggard
# top 5 LONG + 5 SHORT by rs_score
```

**6. Breakdown / Damage (SHORT ONLY — derived from S1 BRF)**
```python
breakdown_score = (
    w1 * normalize(daily_drawdown_from_high) +     # falling hard from daily high
    w2 * normalize(down_volume_ratio) +             # selling volume dominates
    w3 * normalize(-momentum_12bar) +               # strong negative momentum
    w4 * normalize(-structure_break) +              # bearish BOS/CHoCH
    w5 * normalize(-htf_trend_direction) +          # HTF bearish
    w6 * normalize(atr_expansion)                   # volatility expanding (breakdown in progress)
)
# SHORT ONLY — no long equivalent
# Catches stocks breaking down with institutional selling pressure
# S1's BRF (Bearish Reversal Factor) validated this pattern at 71% WR
# Only candidates with lgbm_short_prob > 0.50 enter scoring
# top 5 SHORT by breakdown_score
```

### FOREX Strategies

**1. SMC Confluence** (same formula as equities, forex-optimized weights)

**2. Session Timing**
```python
session_score = (
    w1 * session_quality(session_phase) +      # London/NY overlap = peak
    w2 * normalize(relative_volume) +           # high vol = good session
    w3 * normalize(1 - time_in_session_pct) +   # early in session preferred
    w4 * normalize(spread_proxy_inv)            # tight spread = liquid
)
# session_quality: London/NY overlap = 1.0, London = 0.8,
#                  NY = 0.7, Asian = 0.3, off-hours = 0.0
# top 3 LONG + 3 SHORT
```

**3. HTF Alignment**
```python
htf_score = (
    w1 * normalize(htf_trend_direction) +
    w2 * normalize(htf_structure_break) +
    w3 * normalize(htf_fvg_nearest) +
    w4 * normalize(htf_ob_proximity) +
    w5 * normalize(daily_adx)              # strong daily trend
)
# Only select trades where LTF direction matches HTF
# top 3 LONG + 3 SHORT
```

**4. Liquidity Grab Reversal**
```python
liquidity_grab_score = (
    w1 * normalize(liquidity_sweep) +              # just swept stops
    w2 * normalize(abs(premium_discount_zone - 0.5)) + # at zone extreme
    w3 * normalize(effort_vs_result) +             # volume disagrees with price
    w4 * normalize(-momentum_acceleration) +       # momentum reversing
    w5 * normalize(ob_proximity_5m)                # near order block (entry zone)
)
# LONG: sell-side liquidity swept (stops below taken) + reversal into OB in discount
# SHORT: buy-side liquidity swept (stops above taken) + reversal into OB in premium
# Forex shorts after buy-side liquidity grabs are among the highest
# probability SMC setups — institutions sweep retail stops above equal
# highs then reverse hard
# top 3 LONG + 3 SHORT by liquidity_grab_score
```

**5. Risk-Reward Edge** (same formula as equities)

### INDICES Strategies

**1. Momentum** (same formula as equities)

**2. Session Open**
```python
session_open_score = (
    w1 * normalize(abs(gap_pct)) +                # gap = energy
    w2 * normalize(session_opening_range_position) + # breakout from OR
    w3 * normalize(relative_volume) +               # volume confirms
    w4 * normalize(bars_since_session_open_inv)     # early = better
)
# Only active in first 60 min of session
# top 3 LONG + 3 SHORT
```

**3. Risk-Reward Edge** (same formula)

### METALS Strategies

**1. SMC Confluence** (same formula, gold-optimized weights)

**2. Cross-Asset**
```python
cross_asset_score = (
    w1 * normalize(cross_asset_correlation * -1) +  # inverse DXY
    w2 * normalize(benchmark_momentum_12bar * -1) + # USD weakness = gold strength
    w3 * normalize(daily_conviction)                 # daily system agrees
)
# Gold-specific: strong inverse correlation with USD
# top 3 LONG + 3 SHORT
```

**3. Risk-Reward Edge** (same formula)

### CRYPTO Strategies

**1. SMC Confluence** (same formula, crypto-optimized weights)

**2. Momentum Breakout**
```python
breakout_score = (
    w1 * normalize(momentum_3bar) +
    w2 * normalize(momentum_acceleration) +
    w3 * normalize(atr_expansion) +
    w4 * normalize(volume_burst_z) +
    w5 * normalize(structure_break)           # BOS confirms breakout
)
# Crypto-specific: violent moves with volume + structure break
# top 3 LONG + 3 SHORT
```

**3. Risk-Reward Edge** (same formula)

### ENERGY/AGRICULTURE Strategies

**1. Momentum** (same formula as equities)
**2. Risk-Reward Edge** (same formula)

---

## Direction Handling Reference

Every strategy explicitly handles BOTH long and short directions
(except Breakdown which is SHORT ONLY). The ML gate splits candidates
into sides before any strategy scores them.

### How Each Strategy Handles Direction

| Strategy | LONG Signal | SHORT Signal | Side |
|---|---|---|---|
| **SMC Confluence** | Bullish OB + bullish FVG + bullish BOS + discount zone + buy-side liq sweep | Bearish OB + bearish FVG + bearish CHoCH + premium zone + sell-side liq sweep | BOTH |
| **Momentum** | Positive momentum + acceleration + volume burst up | Negative momentum + acceleration + volume burst down | BOTH |
| **Mean Reversion** | Far below VWAP + deep discount + oversold + volume divergence | Far above VWAP + deep premium + overbought + volume divergence | BOTH |
| **Risk-Reward Edge** | lgbm_long_prob × TP - (1-prob) × SL | lgbm_short_prob × TP - (1-prob) × SL | BOTH |
| **Relative Strength** | High RS vs benchmark in bullish market (leader) | Low RS vs benchmark in bearish market (laggard) | BOTH |
| **Breakdown/Damage** | — | Drawdown from high + down volume + neg momentum + bearish BOS + HTF bearish | **SHORT ONLY** |
| **Session Timing** | ML direction + high session quality + volume + tight spread | ML direction + high session quality + volume + tight spread | BOTH |
| **HTF Alignment** | LTF long matches HTF bullish trend + daily ADX strong | LTF short matches HTF bearish trend + daily ADX strong | BOTH |
| **Liquidity Grab** | Sell-side swept + reversal up into OB in discount | Buy-side swept + reversal down into OB in premium | BOTH |
| **Session Open** | Gap up + OR breakout up + volume confirms | Gap down + OR breakout down + volume confirms | BOTH |
| **Cross-Asset** | DXY weakening + USD bearish momentum (gold bullish) | DXY strengthening + USD bullish momentum (gold bearish) | BOTH |
| **Momentum Breakout** | Violent move up + volume + BOS bullish | Violent move down + volume + BOS bearish | BOTH |

### Key Features for SHORT-Side Signal

These V3 features were specifically designed for short-side detection:
- `down_volume_ratio` — ratio of down-volume to total volume (high = selling pressure)
- `daily_drawdown_from_high` — how far price has fallen from daily high
- Signed OB/FVG features — negative = bearish zones
- `structure_break` — negative = bearish BOS/CHoCH
- `htf_trend_direction` — negative = HTF bearish

### S1 BRF Lineage

The Breakdown/Damage strategy is derived from S1's Bearish Reversal
Factor (BRF), which was validated at 71% directional accuracy on the
short side. BRF identified stocks exhibiting institutional selling
pressure through volume profile + structure damage. Vanguard's
Breakdown strategy extends this with intraday SMC features (bearish
OB/FVG/BOS) that weren't available in S1's daily framework.

---

## Part 3: Consensus Count

After all strategies produce their lists, count how many strategies
selected each instrument:

```python
for instrument in all_selected:
    strategies_matched = [s for s in strategies if instrument in s.top_n]
    consensus_count = len(strategies_matched)
    strategy_names = [s.name for s in strategies_matched]
```

Output per instrument:
```python
{
    "symbol": "AAPL",
    "asset_class": "equity",
    "direction": "LONG",
    "consensus_count": 3,         # out of 5 equity strategies
    "strategies_matched": ["SMC", "MOMENTUM", "RISK_REWARD"],
    "primary_strategy": "MOMENTUM",  # highest rank in this strategy
    "primary_rank": 1,
    "edge_score": 0.15,
    "ml_prob": 0.72,
    "regime": "ACTIVE"
}
```

---

## Part 4: LLM Strategy (Optional, Default OFF)

### When Enabled
Runs after all structured strategies. Receives:
- Top N candidates per asset class (from structured strategies)
- Current regime
- Feature summary (not raw features — interpreted)
- News/sentiment from feed (Yahoo Finance RSS or similar)
- Economic calendar events (upcoming FOMC, NFP, etc.)

### Prompt Template
```
You are reviewing intraday trading candidates for {asset_class}.
Current regime: {regime}
Upcoming events: {events}

Top {N} candidates from structured strategies:
{candidate_table}

Considering qualitative context:
1. Which {top_k} would you select as highest conviction?
2. Any to SKIP due to event risk or qualitative concern?
3. Rank your top {top_k} with confidence 1-10 and one-line reason.

Return JSON only: {"picks": [{"symbol": "...", "confidence": N, "reason": "..."}], "skips": [{"symbol": "...", "reason": "..."}]}
```

### LLM Config
```json
{
    "llm_strategy": {
        "enabled": false,
        "provider": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "news_feed": "yahoo_finance_rss",
        "max_candidates_per_prompt": 10,
        "top_k_picks": 5,
        "timeout_seconds": 30,
        "fallback_on_timeout": "skip"
    }
}
```

---

## Strategy Configuration

### config/vanguard_strategies.json

```json
{
    "equity": {
        "strategies": ["smc", "momentum", "mean_reversion", "risk_reward", "relative_strength", "breakdown"],
        "top_n_per_strategy": 5,
        "short_only_strategies": ["breakdown"],
        "ml_gate_threshold": 0.50
    },
    "forex": {
        "strategies": ["smc", "session_timing", "htf_alignment", "liquidity_grab_reversal", "risk_reward"],
        "top_n_per_strategy": {"smc": 5, "session_timing": 3, "htf_alignment": 3, "liquidity_grab_reversal": 3, "risk_reward": 5},
        "ml_gate_threshold": 0.50
    },
    "index": {
        "strategies": ["momentum", "session_open", "risk_reward"],
        "top_n_per_strategy": 3,
        "ml_gate_threshold": 0.50
    },
    "metal": {
        "strategies": ["smc", "cross_asset", "risk_reward"],
        "top_n_per_strategy": 3,
        "ml_gate_threshold": 0.50
    },
    "crypto": {
        "strategies": ["smc", "momentum_breakout", "risk_reward"],
        "top_n_per_strategy": 3,
        "ml_gate_threshold": 0.50
    },
    "commodity": {
        "strategies": ["momentum", "risk_reward"],
        "top_n_per_strategy": 3,
        "ml_gate_threshold": 0.50
    }
}
```

To add a new strategy: add a scoring function + register in config.
No other code changes needed.

---

## Output Contract

### Table: vanguard_shortlist

```sql
CREATE TABLE IF NOT EXISTS vanguard_shortlist (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    direction TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_rank INTEGER NOT NULL,
    strategy_score REAL NOT NULL,
    ml_prob REAL NOT NULL,
    edge_score REAL,
    consensus_count INTEGER DEFAULT 0,
    strategies_matched TEXT,
    regime TEXT NOT NULL,
    PRIMARY KEY (cycle_ts_utc, symbol, strategy)
);
CREATE INDEX IF NOT EXISTS idx_vg_sl_cycle
    ON vanguard_shortlist(cycle_ts_utc);
CREATE INDEX IF NOT EXISTS idx_vg_sl_asset
    ON vanguard_shortlist(cycle_ts_utc, asset_class);
CREATE INDEX IF NOT EXISTS idx_vg_sl_consensus
    ON vanguard_shortlist(cycle_ts_utc, consensus_count DESC);
```

---

## Performance Target

| Metric | Target |
|---|---|
| Regime gate | < 1 second |
| All strategy scoring functions | < 5 seconds total |
| LLM strategy (when enabled) | < 30 seconds |
| Total V5 | < 10 seconds (without LLM), < 40 seconds (with LLM) |

---

## File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_selection.py           # V5 entry point
├── vanguard/
│   ├── strategies/
│   │   ├── base.py                     # BaseStrategy class
│   │   ├── smc_confluence.py           # SMC scoring (all asset classes)
│   │   ├── momentum.py                 # Momentum scoring (equities, indices, commodities)
│   │   ├── mean_reversion.py           # Mean reversion scoring (equities)
│   │   ├── risk_reward_edge.py         # Edge scoring (all asset classes)
│   │   ├── relative_strength.py        # RS scoring (equities)
│   │   ├── breakdown.py               # SHORT ONLY — structural damage (equities, from S1 BRF)
│   │   ├── session_timing.py           # Forex session scoring
│   │   ├── htf_alignment.py            # HTF alignment scoring (forex)
│   │   ├── liquidity_grab_reversal.py  # Liquidity sweep reversal (forex, metals)
│   │   ├── session_open.py             # Index opening range scoring
│   │   ├── cross_asset.py              # Gold/DXY inverse scoring (metals)
│   │   ├── momentum_breakout.py        # Crypto breakout scoring
│   │   └── llm_strategy.py             # LLM qualitative overlay
│   ├── helpers/
│   │   ├── regime_detector.py          # 5-feature regime logic
│   │   ├── strategy_router.py          # Routes instruments to strategies
│   │   ├── consensus_counter.py        # Counts strategy agreement
│   │   └── normalize.py                # Feature normalization helpers
│   └── __init__.py
├── config/
│   ├── vanguard_strategies.json        # Strategy registry per asset class
│   └── vanguard_selection_config.json   # Thresholds, TBM params
└── tests/
    └── test_vanguard_selection.py
```

---

## CLI

```bash
# Normal — called by orchestrator
python3 stages/vanguard_selection.py

# Dry run
python3 stages/vanguard_selection.py --dry-run

# Specific asset class only
python3 stages/vanguard_selection.py --asset-class equity

# Specific strategy only
python3 stages/vanguard_selection.py --strategy smc

# Force regime
python3 stages/vanguard_selection.py --force-regime ACTIVE

# Enable LLM strategy
python3 stages/vanguard_selection.py --enable-llm

# Debug one symbol
python3 stages/vanguard_selection.py --debug AAPL
```

---

## Acceptance Criteria

- [ ] stages/vanguard_selection.py exists and runs
- [ ] Regime gate using 5 V3 features
- [ ] DEAD/CLOSED regime skips cycle
- [ ] ML gate: all candidates must pass prob > 0.50 (side-specific)
- [ ] EQUITY strategies: SMC, Momentum, Mean Reversion, Risk-Reward, Relative Strength, Breakdown
- [ ] EQUITY Breakdown strategy is SHORT ONLY (derived from S1 BRF)
- [ ] FOREX strategies: SMC, Session Timing, HTF Alignment, Liquidity Grab Reversal, Risk-Reward
- [ ] INDEX strategies: Momentum, Session Open, Risk-Reward
- [ ] METAL strategies: SMC, Cross-Asset, Risk-Reward
- [ ] CRYPTO strategies: SMC, Momentum Breakout, Risk-Reward
- [ ] COMMODITY strategies: Momentum, Risk-Reward
- [ ] ALL bidirectional strategies produce BOTH top N LONG + top N SHORT
- [ ] SHORT-specific features used: down_volume_ratio, daily_drawdown_from_high, signed OB/FVG/BOS
- [ ] Consensus count per instrument across strategies (per direction)
- [ ] LLM strategy (optional, default OFF, configurable)
- [ ] Strategy registry loaded from JSON config
- [ ] short_only_strategies list in config honored
- [ ] Adding new strategy = new scoring function + config entry
- [ ] Writes vanguard_shortlist to DB with strategy column
- [ ] < 10 seconds without LLM
- [ ] No beta stripping anywhere
- [ ] No imports from v2_*.py (Meridian)
