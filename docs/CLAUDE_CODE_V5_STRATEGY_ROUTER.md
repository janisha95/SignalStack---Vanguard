# CLAUDE CODE — Build V5 Strategy Router
# Date: Mar 31, 2026
# Spec: VANGUARD_STAGE_V5_SPEC.md (read it in full from ~/SS/Meridian project knowledge)
# Depends on: V3 factor engine (35 features, 8 modules, 101 tests passing) + V4B LGBM (AUC 0.63/0.57)

---

## READ FIRST — IN THIS EXACT ORDER

1. The full V5 spec:
```bash
cat ~/SS/Meridian/VANGUARD_STAGE_V5_SPEC.md
# OR if not there, check project knowledge files
find ~/SS -name "VANGUARD_STAGE_V5_SPEC.md" 2>/dev/null
```

2. What V3 features are available (V5 consumes these):
```bash
cat ~/SS/Vanguard/config/vanguard_factor_registry.json 2>/dev/null || echo "Check V3 spec for 35 feature list"
```

3. The existing V3 factor engine (to understand output shape):
```bash
head -100 ~/SS/Vanguard/stages/vanguard_factor_engine.py
```

4. The existing V4B model outputs (V5 consumes predictions):
```bash
head -50 ~/SS/Vanguard/stages/vanguard_model_trainer.py
# Check what model predictions look like
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_factor_matrix)" 2>/dev/null
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".tables" 2>/dev/null
```

5. Current Vanguard file structure:
```bash
find ~/SS/Vanguard/vanguard/strategies/ -type f 2>/dev/null || echo "strategies/ dir doesn't exist yet"
ls ~/SS/Vanguard/stages/vanguard_selection.py 2>/dev/null || echo "V5 entry point doesn't exist yet"
ls ~/SS/Vanguard/config/vanguard_strategies.json 2>/dev/null || echo "strategy config doesn't exist yet"
```

Report ALL output before writing any code.

---

## WHAT TO BUILD

V5 Strategy Router as defined in the spec. 3 parts:

### Part 1: Regime Gate
- 5 features from V3 determine if we trade this cycle
- ACTIVE → trade normally
- CAUTION → raise edge threshold
- DEAD → skip cycle (lunch hour + low volume)
- CLOSED → skip cycle (outside session)

### Part 2: Strategy Router (13 strategies)
Each strategy is a scoring function that takes V3 features + V4B predictions
and returns ranked lists of LONG + SHORT candidates.

**EQUITIES (6 strategies):**
1. SMC Confluence — order blocks, FVG, structure breaks, liquidity sweeps
2. Momentum — 3bar + 12bar momentum, acceleration, ATR expansion, volume burst
3. Mean Reversion — VWAP distance, premium/discount extremes, effort vs result
4. Risk-Reward Edge — `(ml_prob × tp) - ((1-ml_prob) × sl) - spread`
5. Relative Strength — RS vs benchmark intraday + daily
6. Breakdown (SHORT ONLY) — drawdown from high, down volume, negative momentum

**FOREX (5), INDICES (3), METALS (3), CRYPTO (3), COMMODITIES (2)**
— see spec for exact formulas.

### Part 3: Consensus Count
After all strategies produce lists, count how many strategies agree per instrument per direction.

---

## FILE STRUCTURE TO CREATE

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_selection.py           # V5 entry point
├── vanguard/
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base.py                     # BaseStrategy class
│   │   ├── smc_confluence.py
│   │   ├── momentum.py
│   │   ├── mean_reversion.py
│   │   ├── risk_reward_edge.py
│   │   ├── relative_strength.py
│   │   ├── breakdown.py               # SHORT ONLY
│   │   ├── session_timing.py           # Forex
│   │   ├── htf_alignment.py            # Forex
│   │   ├── liquidity_grab_reversal.py  # Forex/metals
│   │   ├── session_open.py             # Indices
│   │   ├── cross_asset.py              # Metals (gold/DXY)
│   │   └── momentum_breakout.py        # Crypto
│   ├── helpers/
│   │   ├── regime_detector.py
│   │   ├── strategy_router.py
│   │   ├── consensus_counter.py
│   │   └── normalize.py
├── config/
│   ├── vanguard_strategies.json        # Strategy registry per asset class
│   └── vanguard_selection_config.json
└── tests/
    └── test_vanguard_selection.py
```

---

## ARCHITECTURE RULES

1. **BaseStrategy pattern** — every strategy extends a base class:

```python
class BaseStrategy:
    name: str
    asset_classes: list[str]  # which asset classes this strategy applies to
    short_only: bool = False  # True for Breakdown only

    def score(self, df: pd.DataFrame, direction: str) -> pd.DataFrame:
        """
        Takes feature matrix, returns DataFrame with columns:
        [symbol, direction, strategy_score, strategy_rank]
        Sorted by strategy_score DESC, capped at top_n.
        """
        raise NotImplementedError
```

2. **ML Gate BEFORE strategy scoring** — every candidate must pass
   `lgbm_long_prob > 0.50` (for LONG) or `lgbm_short_prob > 0.50` (for SHORT)
   before entering any strategy. Apply this in the router, not per-strategy.

3. **Normalize helper** — each feature is normalized to 0-1 cross-sectionally
   within the current cycle's survivors:

```python
def normalize(series: pd.Series) -> pd.Series:
    """Cross-sectional normalize to [0, 1]. Handle edge cases."""
    mn, mx = series.min(), series.max()
    if mx == mn:
        return pd.Series(0.5, index=series.index)
    return (series - mn) / (mx - mn)
```

4. **Strategy Router** — routes instruments to strategies by asset class:

```python
def route(instruments_df, strategies_config):
    """
    For each asset class in the config:
    1. Filter instruments to that asset class
    2. Apply ML gate
    3. Run each registered strategy
    4. Collect results
    """
```

5. **Config-driven** — adding a new strategy = new scoring function + entry
   in vanguard_strategies.json. No hardcoded strategy lists in the router.

6. **No imports from v2_*.py** — Vanguard is independent of Meridian.

---

## CRITICAL: HANDLE MISSING V3 FEATURES GRACEFULLY

The V3 factor engine has 35 features across 8 modules. Some strategies
reference features that may be NaN for some instruments (especially SMC
features on equities that don't have clean order blocks). Each strategy
must handle NaN gracefully:

```python
# In each strategy score function:
def score(self, df, direction):
    # Fill NaN with 0 for scoring (absent feature = no signal)
    cols = [self.required_features]
    working = df[cols].fillna(0)
    # ... compute score ...
```

---

## CRITICAL: FEATURE NAME MAPPING

The V5 spec references feature names from the V3 spec. Verify these
match the ACTUAL column names in the vanguard_factor_matrix table.
Common mismatches to watch for:

- Spec says `ob_proximity_5m` → actual column might be `ob_proximity`
- Spec says `fvg_bullish_nearest` → actual might be `fvg_nearest`
- Spec says `premium_discount_zone` → actual might be `premium_discount`

Run this to get the actual column names:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_factor_matrix)" 2>/dev/null
# OR check the factor registry
cat ~/SS/Vanguard/config/vanguard_factor_registry.json 2>/dev/null
```

Map spec names to actual column names BEFORE writing strategy code.

---

## VANGUARD_STRATEGIES.JSON

Create this config file (from the spec §Strategy Configuration):

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

---

## OUTPUT CONTRACT

Write to `vanguard_shortlist` table:

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
```

---

## CLI

```bash
# Normal — called by V7 orchestrator
python3 stages/vanguard_selection.py

# Dry run
python3 stages/vanguard_selection.py --dry-run

# Specific asset class
python3 stages/vanguard_selection.py --asset-class equity

# Specific strategy
python3 stages/vanguard_selection.py --strategy momentum

# Force regime
python3 stages/vanguard_selection.py --force-regime ACTIVE

# Debug one symbol
python3 stages/vanguard_selection.py --debug AAPL
```

---

## TESTS

Write `tests/test_vanguard_selection.py` covering:

1. Regime gate detects all 4 regimes (ACTIVE, CAUTION, DEAD, CLOSED)
2. ML gate filters out candidates below 0.50 threshold
3. Each equity strategy produces top 5 LONG + 5 SHORT (except Breakdown = SHORT only)
4. Breakdown strategy ONLY produces SHORT candidates
5. Risk-Reward Edge formula matches spec
6. Consensus count is correct (symbol in 3 strategies = consensus_count=3)
7. Config-driven: removing a strategy from JSON excludes it
8. NaN features don't crash strategies
9. Empty input (0 survivors) returns empty shortlist, not crash
10. Output written to vanguard_shortlist with correct schema

---

## VERIFY

```bash
# Compile check
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_selection.py

# All strategy files compile
for f in ~/SS/Vanguard/vanguard/strategies/*.py; do
    python3 -m py_compile "$f" && echo "OK: $f" || echo "FAIL: $f"
done

# Run tests
cd ~/SS/Vanguard && python3 -m pytest tests/test_vanguard_selection.py -v

# Dry run
cd ~/SS/Vanguard && python3 stages/vanguard_selection.py --dry-run

# Check output
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT strategy, COUNT(*), AVG(strategy_score) FROM vanguard_shortlist GROUP BY strategy"
```

---

## PERFORMANCE TARGET

- Regime gate: < 1 second
- All strategy scoring: < 5 seconds total
- Total V5: < 10 seconds (without LLM)

---

## GIT

```bash
# Vanguard has no git repo yet — initialize it
cd ~/SS/Vanguard && git init && git add -A && git commit -m "init: V5 Strategy Router build"
```
