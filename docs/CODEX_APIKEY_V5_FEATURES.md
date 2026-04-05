# CODEX — Three Critical Fixes: API Key + V5 Equity Routing + Feature Profiles

## READ FIRST

```bash
# 1. Twelve Data key situation
grep -n "api_key\|API_KEY\|apikey\|load_dotenv\|\.env" ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py | head -20
cat ~/SS/Vanguard/.env | grep -i twelve

# 2. V5 strategy routing for equity — why 0 results?
grep -n "equity\|EQUITY\|strategy.*equity\|register\|route" ~/SS/Vanguard/stages/vanguard_selection.py | head -20
grep -n "equity\|EQUITY\|register" ~/SS/Vanguard/vanguard/helpers/strategy_router.py | head -20
ls ~/SS/Vanguard/vanguard/strategies/

# 3. Current feature columns used in training
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_training_data)" | head -50

# 4. Model trainer feature handling
grep -n "feature\|column\|drop\|nan_ratio\|spread_proxy\|time_in_session" ~/SS/Vanguard/stages/vanguard_model_trainer.py | head -20

# 5. What features the trained models used (from logs)
cat ~/SS/Vanguard/logs/v4b_*.log 2>/dev/null | grep -A 15 "Top 10 features"
```

**Report ALL output before making any changes.**

---

## FIX 1: Twelve Data API Key — Permanent Fix

The API key exists in `~/SS/Vanguard/.env` but doesn't reliably load across terminal sessions, nohup processes, and launchd jobs.

### Root cause
The adapter was patched (commit `498986b`) to fall back to `.env`, but the fallback may not handle all cases: new terminals, subprocess spawning, different working directories.

### Fix: Belt-and-suspenders approach

**A.** In `twelvedata_adapter.py`, make the `.env` loading bulletproof:

```python
import os
from pathlib import Path

def _load_twelve_data_key() -> str:
    """Load Twelve Data API key with multiple fallback paths."""
    # 1. Process environment (highest priority — set by export or launchd)
    key = os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY")
    if key:
        return key

    # 2. Vanguard .env file (relative to this file's location)
    env_paths = [
        Path(__file__).resolve().parents[2] / ".env",          # ~/SS/Vanguard/.env
        Path.home() / "SS" / "Vanguard" / ".env",              # absolute fallback
        Path.home() / "SS" / ".env",                            # shared .env
    ]
    for env_path in env_paths:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k in ("TWELVE_DATA_API_KEY", "TWELVEDATA_API_KEY"):
                    os.environ[k] = v  # Also set in env for child processes
                    return v

    return ""
```

**B.** Call this at module level and at poll time:

```python
# At module level (loaded on import)
_API_KEY = _load_twelve_data_key()

# Inside poll_latest_bars(), re-check if key was loaded
def poll_latest_bars(self):
    global _API_KEY
    if not _API_KEY:
        _API_KEY = _load_twelve_data_key()
    if not _API_KEY:
        logger.error("[twelvedata] No API key found in env or .env files")
        return 0
    # ... rest of polling logic using _API_KEY
```

**C.** Add the key to shell profile so new terminals always have it:

```bash
# Check if already in shell profile
grep -l "TWELVE_DATA_API_KEY" ~/.zshrc ~/.zprofile ~/.bashrc ~/.bash_profile 2>/dev/null

# If not found in any profile, add it
TWELVE_KEY=$(grep "TWELVE_DATA_API_KEY" ~/SS/Vanguard/.env | cut -d'=' -f2 | tr -d "'" | tr -d '"')
if [ -n "$TWELVE_KEY" ]; then
    echo "export TWELVE_DATA_API_KEY='$TWELVE_KEY'" >> ~/.zshrc
    echo "Added TWELVE_DATA_API_KEY to ~/.zshrc"
fi
```

**D.** For launchd plist jobs (future automation), add to the plist:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>TWELVE_DATA_API_KEY</key>
    <string>YOUR_KEY_HERE</string>
</dict>
```

---

## FIX 2: V5 Strategy Router Returns 0 for Equity

Running `--single-cycle --force-regime ACTIVE` with 28 equity survivors produces:
```
Strategy routing done in 0.01s — 0 result batches
No results from strategy router
```

### Diagnose

```bash
# What does the strategy router do with equity symbols?
cat ~/SS/Vanguard/vanguard/helpers/strategy_router.py

# What strategies are registered for equity?
grep -n "equity\|register\|ASSET_CLASS" ~/SS/Vanguard/vanguard/helpers/strategy_router.py

# What does V5 pass to the router?
grep -n "strategy_router\|route\|run_strategies\|router" ~/SS/Vanguard/stages/vanguard_selection.py | head -20

# Is there an ML gate that kills equity before routing?
grep -n "ml_gate\|threshold\|prob.*0\.\|gate\|filter" ~/SS/Vanguard/stages/vanguard_selection.py | head -20

# What are the trained equity model probabilities?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT model_id, readiness, mean_ic_long, mean_ic_short
FROM vanguard_model_registry WHERE asset_class='equity'
"
```

### Likely causes (check in this order):

**A. ML gate kills all equity candidates before strategy routing**
The equity model IC is 0.001 — probabilities are probably clustered around 0.50. If the ML gate threshold is 0.50, half pass. If 0.55, almost none pass.

**B. Strategy router doesn't have equity strategies registered**
The forex/crypto/metal E2E test produced results because those asset class strategies may be registered, but equity strategies may not be mapped in the router.

**C. The equity model `readiness=trained_insufficient_validation` sets an impossible threshold**
Same issue as the FTMO `readiness=not_trained → threshold=1.0` from last night, but for equity models.

### Fix based on diagnosis:

If **A** (ML gate): The mock ML fallback (commit `efc2f14`) should still trigger when real model probs are bad. Check if the mock fallback logic only activates when NO model exists vs when the model exists but is garbage.

If **B** (strategy registration): Register equity strategies in the strategy router. The V5 spec defines 6 equity strategies: SMC Confluence, Momentum, Mean Reversion, Risk-Reward Edge, Relative Strength, Breakdown.

If **C** (readiness threshold): Change `trained_insufficient_validation` to use the normal threshold (0.50), not an elevated one. This readiness state means "model exists but IC is below production standard" — it should still produce candidates for testing, just with a warning.

---

## FIX 3: Per-Asset Feature Profiles for Model Training

### Problem
The current V4B trainer feeds ALL columns from `vanguard_training_data` to LightGBM. This includes noise features that the model latches onto:
- `time_in_session_pct` — session timing metadata, not price signal
- `spread_proxy` — data quality indicator, not directional signal
- `nan_ratio` — THIS SHOULD NOT EVEN BE A TRAINING FEATURE
- `asset_class_encoded` — constant within per-asset training (always the same value)

The Meridian TCN was successful with 19 curated features. The Vanguard LGBM needs the same discipline.

### Create `config/vanguard_feature_profiles.json`

```json
{
    "_note": "Per-asset-class feature profiles for V4B model training. V3 computes all 35 features for all symbols. These profiles control which features each asset class model TRAINS on.",

    "equity": {
        "features": [
            "session_vwap_distance",
            "session_opening_range_position",
            "gap_pct",
            "dist_from_session_high_low",
            "momentum_3bar",
            "momentum_12bar",
            "momentum_acceleration",
            "atr_expansion",
            "relative_volume",
            "volume_burst_z",
            "effort_vs_result",
            "down_volume_ratio",
            "rs_vs_benchmark_intraday",
            "benchmark_momentum_12bar",
            "daily_atr_pct",
            "daily_rs_vs_benchmark",
            "daily_drawdown_from_high",
            "daily_adx",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "ob_proximity_5m",
            "structure_break",
            "premium_discount_zone",
            "htf_trend_direction",
            "htf_structure_break"
        ],
        "exclude_reason": "Removed: time_in_session_pct (metadata), spread_proxy (quality indicator), nan_ratio (not a feature), asset_class_encoded (constant), bars_since_session_open (correlated with time_in_session_pct), daily_conviction (always 0), cross_asset_correlation (needs tuning), liquidity_sweep (sparse), htf_fvg_nearest (sparse), htf_ob_proximity (sparse)",
        "count": 25
    },

    "forex": {
        "features": [
            "session_vwap_distance",
            "dist_from_session_high_low",
            "momentum_3bar",
            "momentum_12bar",
            "momentum_acceleration",
            "atr_expansion",
            "effort_vs_result",
            "rs_vs_benchmark_intraday",
            "benchmark_momentum_12bar",
            "cross_asset_correlation",
            "daily_atr_pct",
            "daily_rs_vs_benchmark",
            "daily_drawdown_from_high",
            "daily_adx",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "ob_proximity_5m",
            "structure_break",
            "liquidity_sweep",
            "premium_discount_zone",
            "htf_trend_direction",
            "htf_structure_break",
            "htf_fvg_nearest",
            "htf_ob_proximity",
            "session_phase"
        ],
        "exclude_reason": "Removed: gap_pct (no gaps in 24/5), session_opening_range_position (ambiguous for forex), relative_volume (tick volume unreliable), volume_burst_z (tick volume), down_volume_ratio (tick volume), time_in_session_pct (metadata), spread_proxy (quality), nan_ratio (not a feature), asset_class_encoded (constant), bars_since_session_open (metadata), daily_conviction (always 0)",
        "count": 25
    },

    "crypto": {
        "features": [
            "dist_from_session_high_low",
            "momentum_3bar",
            "momentum_12bar",
            "momentum_acceleration",
            "atr_expansion",
            "effort_vs_result",
            "daily_atr_pct",
            "daily_drawdown_from_high",
            "daily_adx",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "ob_proximity_5m",
            "structure_break",
            "liquidity_sweep",
            "premium_discount_zone",
            "htf_trend_direction",
            "htf_structure_break",
            "htf_fvg_nearest",
            "htf_ob_proximity"
        ],
        "exclude_reason": "Removed: session_vwap_distance (no clear session for 24/7), session_opening_range_position (no session open), gap_pct (24/7), all volume features (tick volume unreliable for crypto), all benchmark features (crypto is self-referential), session_phase (no sessions), time_in_session_pct (no sessions), spread_proxy (quality), nan_ratio (not a feature), asset_class_encoded (constant), daily_conviction (always 0), cross_asset_correlation (needs real vol), bars_since_session_open (no sessions)",
        "count": 19
    },

    "metal": {
        "features": [
            "session_vwap_distance",
            "dist_from_session_high_low",
            "momentum_3bar",
            "momentum_12bar",
            "momentum_acceleration",
            "atr_expansion",
            "effort_vs_result",
            "rs_vs_benchmark_intraday",
            "benchmark_momentum_12bar",
            "cross_asset_correlation",
            "daily_atr_pct",
            "daily_rs_vs_benchmark",
            "daily_drawdown_from_high",
            "daily_adx",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "ob_proximity_5m",
            "structure_break",
            "premium_discount_zone",
            "htf_trend_direction",
            "htf_structure_break",
            "session_phase",
            "gap_pct"
        ],
        "exclude_reason": "Removed: volume features (tick volume unreliable), time_in_session_pct (metadata), spread_proxy (quality), nan_ratio (not a feature), asset_class_encoded (constant), daily_conviction (always 0), bars_since_session_open (metadata), session_opening_range_position (ambiguous), htf_fvg_nearest (sparse), htf_ob_proximity (sparse), liquidity_sweep (sparse)",
        "count": 23
    },

    "commodity": {
        "features": [
            "session_vwap_distance",
            "dist_from_session_high_low",
            "momentum_3bar",
            "momentum_12bar",
            "momentum_acceleration",
            "atr_expansion",
            "daily_atr_pct",
            "daily_drawdown_from_high",
            "daily_adx",
            "fvg_bullish_nearest",
            "fvg_bearish_nearest",
            "ob_proximity_5m",
            "structure_break",
            "premium_discount_zone",
            "htf_trend_direction",
            "session_phase",
            "gap_pct"
        ],
        "exclude_reason": "Same as metal plus fewer benchmark features (no clear benchmark for agriculture/energy commodities)",
        "count": 17
    },

    "universal_exclude": [
        "nan_ratio",
        "asset_class_encoded",
        "daily_conviction",
        "time_in_session_pct",
        "bars_since_session_open",
        "spread_proxy"
    ],
    "universal_exclude_reason": "These are never useful training features for any asset class: nan_ratio is a data quality metric, asset_class_encoded is constant in per-asset training, daily_conviction is always 0, time_in_session_pct/bars_since_session_open are session metadata not price signal, spread_proxy is a data quality indicator"
}
```

### Update V4B model trainer to use feature profiles

In `stages/vanguard_model_trainer.py`:

```python
import json
from pathlib import Path

def load_feature_profile(asset_class: str) -> list[str]:
    """Load curated feature list for this asset class."""
    config_path = Path(__file__).parent.parent / "config" / "vanguard_feature_profiles.json"
    if not config_path.exists():
        logger.warning(f"[V4B] No feature profile config — using all features")
        return None  # Use all features (backward compatible)

    profiles = json.loads(config_path.read_text())

    # Get asset-class-specific profile
    profile = profiles.get(asset_class)
    if not profile:
        logger.warning(f"[V4B] No feature profile for {asset_class} — using all features minus universal excludes")
        return None

    features = profile["features"]
    logger.info(f"[V4B] Feature profile for {asset_class}: {len(features)} features")
    return features


# In the training function, BEFORE training:
def train_model(df, asset_class, direction, label_col):
    # Load curated feature list
    feature_list = load_feature_profile(asset_class)

    if feature_list:
        # Use only curated features
        available = [f for f in feature_list if f in df.columns]
        missing = [f for f in feature_list if f not in df.columns]
        if missing:
            logger.warning(f"[V4B] {asset_class}: {len(missing)} profile features missing from data: {missing}")
        X = df[available]
    else:
        # Fallback: use all numeric columns minus universal excludes
        universal_exclude = ["nan_ratio", "asset_class_encoded", "daily_conviction",
                            "time_in_session_pct", "bars_since_session_open", "spread_proxy"]
        feature_cols = [c for c in df.select_dtypes(include='number').columns
                       if c not in universal_exclude
                       and c not in (label_col, "label_long", "label_short",
                                    "forward_return", "entry_price", "exit_bar",
                                    "max_favorable_excursion", "max_adverse_excursion",
                                    "truncated", "warm_up", "horizon_bars", "tp_pct", "sl_pct")]
        X = df[feature_cols]

    y = df[label_col]

    logger.info(f"[V4B] Training {asset_class}_{direction}: {len(X)} rows × {X.shape[1]} features")
    logger.info(f"[V4B] Features: {list(X.columns)}")

    # ... rest of training logic
```

### IMPORTANT: Also exclude noise from the universal fallback

Even without the feature profile config, the trainer should NEVER include:
- `nan_ratio` — not a real feature
- `asset_class_encoded` — constant in per-asset training
- `daily_conviction` — always 0 (placeholder for future integration)
- Label columns, entry price, TBM params, metadata columns

Check if `nan_ratio` is in the training data schema:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_training_data)" | grep -i "nan"
```

If it's there, it was computed by V4A as a quality metric and should NOT be fed to the model.

---

## VERIFY

```bash
# Fix 1: API key loads in a fresh terminal
/bin/zsh -c 'cd ~/SS/Vanguard && python3 -c "
from vanguard.data_adapters.twelvedata_adapter import _load_twelve_data_key
k = _load_twelve_data_key()
print(f\"Key present: {bool(k)}, prefix: {k[:8]}...\")
"'

# Fix 2: V5 produces results for equity
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --force-regime ACTIVE 2>&1 | grep -E "\[V5\]|strategy|routing|result|candidates|equity"

# Fix 3: Feature profiles loaded
cd ~/SS/Vanguard && python3 -c "
import json
p = json.load(open('config/vanguard_feature_profiles.json'))
for ac in ['equity','forex','crypto','metal']:
    print(f'{ac}: {p[ac][\"count\"]} features')
"

# After retrain with feature profiles:
cd ~/SS/Vanguard && python3 stages/vanguard_model_trainer.py 2>&1 | grep -E "Feature profile|features|IC|Top 10"
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: permanent API key loading, V5 equity routing, per-asset feature profiles for V4B"
```
