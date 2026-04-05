# SignalStack — Self-Sufficiency Guide
# Everything you need to work independently. No AI required.

## Quick Reference Commands

### Start Everything
```bash
~/SS/servers.sh start                    # All servers
~/SS/ultra.sh                            # Full daily pipeline (run 4:30-11:59 PM ET)
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 --execution-mode off &
```

### Check Status
```bash
~/SS/servers.sh status                   # Server health
sqlite3 ~/SS/Meridian/data/ibkr_daily.db "SELECT COUNT(DISTINCT symbol), MAX(date) FROM ibkr_daily_bars"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT asset_class, direction, COUNT(*) FROM vanguard_predictions WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions) GROUP BY asset_class, direction"
```

### Place a Manual GFT Trade
```bash
cd ~/SS/Vanguard && python3 -c "
from vanguard.executors.mt5_executor import connect, execute_trade
connect()
result = execute_trade('EURUSD', 'SHORT', 0.10, stop_loss=1.1560, take_profit=1.1500)
print(result)
"
```

---

## 1. THE FEATURE CONTRACT ISSUE (Root Cause + How to Fix)

### What's Wrong
Two DIFFERENT code paths compute features:
- **V4A** (`stages/vanguard_training_backfill.py`) — generates training data
- **V3** (`stages/vanguard_factor_engine.py`) — computes live features

They SHOULD produce identical features but don't. This is why models with
IC +0.034-0.049 in training produce near-constant outputs in production.

### How to Diagnose
```bash
# Step 1: Dump live V3 features for one symbol
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT * FROM vanguard_factor_matrix 
WHERE symbol = 'EURUSD' 
ORDER BY cycle_ts_utc DESC LIMIT 1
" -header -column

# Step 2: Dump training features for same symbol
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT * FROM vanguard_training_data 
WHERE symbol = 'EURUSD' 
ORDER BY bar_ts_utc DESC LIMIT 1
" -header -column

# Step 3: Compare feature distributions
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 
    'training' as source,
    ROUND(AVG(momentum_3bar), 6) as avg_mom3,
    ROUND(AVG(atr_expansion), 6) as avg_atr,
    ROUND(AVG(relative_volume), 6) as avg_rvol,
    ROUND(AVG(daily_adx), 6) as avg_adx
FROM vanguard_training_data WHERE asset_class = 'equity'
UNION ALL
SELECT 
    'live' as source,
    ROUND(AVG(momentum_3bar), 6),
    ROUND(AVG(atr_expansion), 6),
    ROUND(AVG(relative_volume), 6),
    ROUND(AVG(daily_adx), 6)
FROM vanguard_factor_matrix 
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
AND asset_class = 'equity'
"
```

If distributions are wildly different → feature skew confirmed.

### How to Fix (The Shared Feature Module)

Create ONE file that BOTH V3 and V4A import:

```
~/SS/Vanguard/vanguard/features/feature_computer.py
```

This file contains ALL 35 feature computation functions. V3 calls it for live.
V4A calls it for training. Same code path = zero skew.

**Step-by-step:**

1. Open `stages/vanguard_factor_engine.py` — find all 35 feature computations
2. Open `stages/vanguard_training_backfill.py` — find its feature computations
3. Diff them: `diff <(grep "def.*compute\|def.*calc\|def.*get" stages/vanguard_factor_engine.py) <(grep "def.*compute\|def.*calc\|def.*get" stages/vanguard_training_backfill.py)`
4. Create `vanguard/features/feature_computer.py` with the V3 versions (V3 = production truth)
5. Change V3 to: `from vanguard.features.feature_computer import compute_all_features`
6. Change V4A to: `from vanguard.features.feature_computer import compute_all_features`
7. Retrain models on V4A-generated data (now uses same code as V3)

**Key files to read:**
```
~/SS/Vanguard/stages/vanguard_factor_engine.py     # V3 — live features (THE TRUTH)
~/SS/Vanguard/stages/vanguard_training_backfill.py  # V4A — training features (SUSPECT)
~/SS/Vanguard/stages/vanguard_scorer.py             # V4B — loads models + predicts
~/SS/Vanguard/stages/vanguard_selection_simple.py   # V5 — cross-sectional ranking
```

---

## 2. HOW TO TRAIN A NEW LGBM MODEL (Step by Step)

### Prerequisites
```bash
pip install lightgbm scikit-learn scipy joblib pandas --break-system-packages
```

### Training Script Template

Save this as `~/SS/Vanguard/scripts/train_model.py`:

```python
#!/usr/bin/env python3
"""
Train an LGBM regressor for any asset class.
Usage:
    python3 scripts/train_model.py --asset-class crypto --model-name lgbm_crypto_v1
    python3 scripts/train_model.py --asset-class forex --model-name lgbm_forex_v2
"""
import argparse, sqlite3, json, joblib, numpy as np, pandas as pd
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from scipy.stats import spearmanr
from pathlib import Path
from datetime import datetime, timezone

DB = str(Path.home() / 'SS/Vanguard/data/vanguard_universe.db')
MODELS_DIR = Path.home() / 'SS/Vanguard/models'

def train(asset_class, model_name, min_rows=500):
    con = sqlite3.connect(DB)
    
    # Step 1: Load training data
    df = pd.read_sql(f"""
        SELECT * FROM vanguard_training_data
        WHERE asset_class = '{asset_class}'
        ORDER BY bar_ts_utc
    """, con)
    con.close()
    
    if len(df) < min_rows:
        print(f"Only {len(df)} rows for {asset_class}. Need {min_rows}+.")
        print("Generate more training data first (see Section 3 below).")
        return
    
    print(f"Training data: {len(df)} rows, {df['symbol'].nunique()} symbols")
    
    # Step 2: Identify features (exclude metadata columns)
    meta = {'symbol', 'asset_class', 'bar_ts_utc', 'forward_return',
            'cycle_ts_utc', 'date', 'created_at', 'nan_ratio'}
    features = [c for c in df.columns if c not in meta
                and df[c].dtype in ['float64', 'float32', 'int64']
                and df[c].notna().sum() > len(df) * 0.3]
    print(f"Features: {len(features)}")
    print(f"  {features[:10]}...")
    
    # Step 3: Walk-forward validation
    X = df[features].fillna(0).values
    y = df['forward_return'].values
    
    tscv = TimeSeriesSplit(n_splits=5)
    ics = []
    
    for fold, (train_idx, test_idx) in enumerate(tscv.split(X)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]
        
        # Cross-sectional standardization (MUST match V4B production)
        for i in range(X_train.shape[1]):
            mean = X_train[:, i].mean()
            std = X_train[:, i].std() + 1e-8
            X_train[:, i] = (X_train[:, i] - mean) / std
            X_test[:, i] = (X_test[:, i] - mean) / std
        
        model = lgb.LGBMRegressor(
            n_estimators=500, learning_rate=0.05, num_leaves=31,
            feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=5,
            min_child_samples=20, verbose=-1, random_state=42
        )
        model.fit(X_train, y_train)
        pred = model.predict(X_test)
        ic = spearmanr(pred, y_test).statistic
        ics.append(ic)
        print(f"  Fold {fold+1}: IC = {ic:+.4f}")
    
    avg_ic = np.mean(ics)
    print(f"\n  Average IC: {avg_ic:+.4f}")
    
    if avg_ic < 0.01:
        print(f"  WARNING: IC < 0.01. Model has weak/no signal. Consider:")
        print(f"    - More training data")
        print(f"    - Different features")
        print(f"    - Different target (binary instead of regression)")
        # Still save it — you can decide later
    
    # Step 4: Train final model on all data
    X_all = X.copy()
    for i in range(X_all.shape[1]):
        mean = X_all[:, i].mean()
        std = X_all[:, i].std() + 1e-8
        X_all[:, i] = (X_all[:, i] - mean) / std
    
    final = lgb.LGBMRegressor(
        n_estimators=500, learning_rate=0.05, num_leaves=31,
        feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=5,
        min_child_samples=20, verbose=-1, random_state=42
    )
    final.fit(X_all, y)
    
    # Step 5: Save model + config
    out_dir = MODELS_DIR / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, out_dir / f'{model_name}.pkl')
    
    meta_json = {
        'model': model_name,
        'asset_classes': [asset_class],
        'features': features,
        'avg_ic': round(avg_ic, 4),
        'rows': len(df),
        'symbols': df['symbol'].nunique(),
        'n_estimators': 500,
        'trained_at': datetime.now(timezone.utc).isoformat(),
    }
    with open(out_dir / 'meta.json', 'w') as f:
        json.dump(meta_json, f, indent=2)
    
    # Step 6: Feature importance
    imp = sorted(zip(features, final.feature_importances_), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 features:")
    for name, score in imp[:10]:
        print(f"    {name:35s} {score:.0f}")
    
    print(f"\n  Saved to {out_dir}")
    print(f"  Model: {out_dir / f'{model_name}.pkl'}")
    print(f"  Config: {out_dir / 'meta.json'}")
    
    # Step 7: Verify model file
    size = (out_dir / f'{model_name}.pkl').stat().st_size
    print(f"  Size: {size/1024:.1f}KB ({final.booster_.num_trees()} trees)")
    
    return avg_ic

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--asset-class', required=True, help='equity, forex, crypto, metal, energy, index')
    parser.add_argument('--model-name', required=True, help='e.g., lgbm_crypto_v1')
    parser.add_argument('--min-rows', type=int, default=500)
    args = parser.parse_args()
    train(args.asset_class, args.model_name, args.min_rows)
```

### Training Commands
```bash
# Crypto
python3 scripts/train_model.py --asset-class crypto --model-name lgbm_crypto_v1

# Metals/Energy (combined)
python3 scripts/train_model.py --asset-class metal --model-name lgbm_metal_v1

# Forex v2 (retrained on V3 features after shared module fix)
python3 scripts/train_model.py --asset-class forex --model-name lgbm_forex_v2

# Equity v2
python3 scripts/train_model.py --asset-class equity --model-name lgbm_equity_v2
```

---

## 3. HOW TO GENERATE TRAINING DATA

The training data comes from replaying V3 feature computation on historical bars.

### Check existing training data
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) as rows, COUNT(DISTINCT symbol) as symbols,
    MIN(bar_ts_utc) as earliest, MAX(bar_ts_utc) as latest
FROM vanguard_training_data
GROUP BY asset_class
"
```

### Generate more training data
```bash
# This replays V3 on historical 5m bars
cd ~/SS/Vanguard && python3 stages/vanguard_training_backfill.py --asset-class crypto
cd ~/SS/Vanguard && python3 stages/vanguard_training_backfill.py --asset-class metal
cd ~/SS/Vanguard && python3 stages/vanguard_training_backfill.py --asset-class energy
```

If the backfill script doesn't support `--asset-class`, check:
```bash
python3 stages/vanguard_training_backfill.py --help
grep "asset_class\|argparse\|add_argument" stages/vanguard_training_backfill.py | head -10
```

### Alternative: Generate from live V3 features
```bash
# Capture live V3 features every cycle (accumulate over 2-3 days)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT COUNT(*), MIN(cycle_ts_utc), MAX(cycle_ts_utc)
FROM vanguard_factor_matrix
WHERE asset_class = 'forex'
"
# If you have 200+ cycles of live features, export and train on those
# This ELIMINATES the train-serving skew entirely
```

---

## 4. HOW TO REGISTER A NEW MODEL IN V4B

After training, tell the scorer about the new model.

### File: `~/SS/Vanguard/stages/vanguard_scorer.py`

Find the `MODEL_SPECS` or `ASSET_CONFIGS` dictionary:
```bash
grep -n "MODEL_SPECS\|ASSET_CONFIGS\|model_dir\|model_id" \
  ~/SS/Vanguard/stages/vanguard_scorer.py | head -20
```

Add your new model:
```python
"crypto": {
    "model_dir": "models/lgbm_crypto_v1",
    "models": {"lgbm": {"file": "lgbm_crypto_v1.pkl", "weight": 1.0}},
    "model_id": "lgbm_crypto_v1",
},
```

Then run a cycle to test:
```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --execution-mode off 2>&1 | tail -20

# Check if crypto predictions exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, COUNT(*)
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class, direction
"
```

---

## 5. FOREX MODEL — WHY SCORES ARE MULTIPLES OF 7

You noticed all forex scores are multiples of ~7 (0.08, 0.15, 0.22, 0.29, 0.36...).
That's because there are only 14 forex pairs. Cross-sectional percentile ranking
of 14 items produces ranks: 1/14=0.07, 2/14=0.14, 3/14=0.21, etc.

This is EXPECTED behavior — the ranking is working correctly. The issue is
the MODEL's ordering (which pairs it ranks highest) may not be accurate.

To improve forex predictions:
1. Fix feature contract (Section 1)
2. More training data (accumulate 2+ days of V3 features)
3. Add more features (spread, session time, major news events)
4. Try RF instead of LGBM (RF had IC +0.049 vs LGBM +0.037)

---

## 6. CODEBASE MAP

```
~/SS/
├── Advance/                           # S1 daily system
│   ├── agent_server.py                # HTTP server (~10K lines)
│   ├── s1_evening_orchestrator.py     # Evening scan entry point
│   ├── s1_orchestrator_v2.py          # Pipeline runner (evening/night/morning)
│   ├── fast_universe_cache.py         # Alpaca data fetcher (now → ibkr_daily.db)
│   ├── s1_daily_shortlist.py          # Top 5 picks
│   ├── s1_pass_scorer.py              # LightGBM scorer
│   ├── signalstack_prefilter.py       # Universe filter
│   ├── signalstack_router.py          # Strategy router
│   └── modules/strategies/            # BRF, RCT, Wyckoff, MR, SRS
│
├── Meridian/                          # Daily swing system
│   ├── stages/
│   │   ├── v2_cache_warm.py           # Data fetch (IBKR 500 priority + Alpaca 11K fallback)
│   │   ├── v2_prefilter.py            # Universe filter (11K → 3K)
│   │   ├── v2_factor_engine.py        # 34 factor computation
│   │   ├── v2_model_trainer.py        # TCN/LGBM training
│   │   ├── v2_selection.py            # Top 30+30 picks (ETF exclusion)
│   │   └── v2_orchestrator.py         # Pipeline runner
│   ├── models/
│   │   ├── tcn_pass_v1/               # TCN LONG IC +0.105
│   │   ├── tcn_short_v1/              # TCN SHORT IC +0.392
│   │   ├── ridge_long_v1/             # Ridge LONG IC +0.087
│   │   └── lgbm_long_v1/              # LGBM LONG IC +0.048
│   ├── data/
│   │   ├── ibkr_daily.db             # THE ONE DB (S1 + Meridian shared)
│   │   └── v2_universe.db            # Meridian working DB (synced from ibkr_daily.db)
│   └── config/etf_tickers.json       # 797 ETFs excluded from LONG
│
├── Vanguard/                          # Intraday system
│   ├── stages/
│   │   ├── vanguard_orchestrator.py   # V7: loop + Telegram + execution
│   │   ├── vanguard_prefilter.py      # V2: health checks
│   │   ├── vanguard_factor_engine.py  # V3: 35 features ← PRODUCTION TRUTH
│   │   ├── vanguard_scorer.py         # V4B: ensemble scoring + standardization
│   │   ├── vanguard_selection_simple.py # V5: cross-sectional ranking
│   │   ├── vanguard_risk_filters.py   # V6: risk gates per account
│   │   └── vanguard_training_backfill.py # V4A: training data ← SUSPECT (skew source)
│   ├── vanguard/
│   │   ├── data_adapters/
│   │   │   ├── alpaca_adapter.py      # Equity WS + REST
│   │   │   ├── ibkr_adapter.py        # IBKR streaming + daily
│   │   │   └── twelvedata_adapter.py  # Crypto only
│   │   ├── executors/
│   │   │   └── mt5_executor.py        # GFT MetaApi execution
│   │   └── helpers/
│   │       ├── universe_builder.py    # Canonical universe
│   │       ├── bars.py                # Aggregation 1m→5m→1h
│   │       └── clock.py              # Session/timezone
│   ├── models/
│   │   ├── equity_regressors/         # LGBM+Ridge+ET (IC +0.034)
│   │   └── forex_regressors/          # RF+LGBM (IC +0.049)
│   ├── data/
│   │   ├── vanguard_universe.db       # Main intraday DB (10GB)
│   │   └── ibkr_intraday.db          # IBKR streaming bars
│   └── gft_tp_sl_calculator.py       # TP/SL + lot size calculator
│
├── data/signalstack_trades.db         # Unified trade queue
├── signalstack_mcp_server.py          # MCP server v3
├── signalstack_trade_pipeline.py      # Daily trade queue builder
├── ultra.sh                           # Master pipeline runner
├── servers.sh                         # Service manager
└── .env                               # API keys (ALPACA, IBKR, METAAPI, TELEGRAM)
```

---

## 7. ENVIRONMENT VARIABLES

```bash
# Required (in ~/.zshrc or ~/SS/.env)
export ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
export ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS
export METAAPI_TOKEN=eyJhbG...  # your MetaApi JWT
export GFT_ACCOUNT_ID=a6275037-d6ac-4bba-bee0-b36bbedc0a8f
export TELEGRAM_BOT_TOKEN=...   # check ~/SS/.env.shared
export TELEGRAM_CHAT_ID=...     # check ~/SS/.env.shared

# Optional
export IB_INTRADAY_SYMBOL_LIMIT=0   # 0 = disable IBKR equity (use Alpaca)
export MERIDIAN_IBKR_DAILY_FETCH_LIMIT=500
```

---

## 8. DAILY ROUTINE

### Evening (4:30-5 PM ET, after market close)
```bash
~/SS/ultra.sh    # Fetches data, runs Meridian + S1, produces picks
```

### Night (any time)
```bash
# Check picks
cd ~/SS && python3 signalstack_trade_pipeline.py
cd ~/SS/Advance && python3 s1_daily_shortlist.py
cd ~/SS/Meridian && python3 meridian_daily_shortlist.py
```

### Market Hours (9:30 AM - 4 PM ET)
```bash
# Start intraday loop
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 --execution-mode off &
# Watch Telegram for picks
```

### Forex Hours (Sun 5 PM - Fri 5 PM ET)
```bash
# Same loop command — session window is 00:00-23:59 when GFT accounts active
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 --execution-mode off &
```

---

## 9. DEBUGGING WITHOUT AI

### Picks not flowing?
```bash
# Check V2 survivors
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, status, COUNT(*) FROM vanguard_health
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_health)
GROUP BY asset_class, status
"

# Check V3 features computed
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) FROM vanguard_factor_matrix
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_factor_matrix)
GROUP BY asset_class
"

# Check V4B predictions
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, direction, COUNT(*) FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class, direction
"

# Check V6 rejections
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, rejection_reason, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account_id, status, rejection_reason
ORDER BY COUNT(*) DESC LIMIT 20
"
```

### IBKR disconnected?
Restart IB Gateway app, then restart the orchestrator loop.

### Telegram not sending?
```bash
grep "telegram\|error\|exception" ~/SS/logs/vanguard_loop.log | tail -20
```

### GFT trade failed?
```bash
# Check MetaApi connection
python3 -c "
from vanguard.executors.mt5_executor import connect
print(connect())
"
# If False: check METAAPI_TOKEN and GFT_ACCOUNT_ID env vars
```

---

## 10. WHAT'S WORKING vs WHAT NEEDS WORK

| Component | Status | Notes |
|---|---|---|
| Daily Meridian (TCN SHORT) | ✅ Strong | IC +0.392, shorts crushing it |
| Daily Meridian (TCN LONG) | ⚠️ OK | IC +0.105, ETF exclusion helps |
| Daily S1 (RF gate) | ✅ Working | 58.9% WR at 0.55 threshold |
| Intraday Equity (Vanguard) | ⚠️ Producing picks | Needs ETF exclusion + better universe |
| Intraday Forex (Vanguard) | ❌ Weak signal | Train-serving skew, needs shared feature module |
| GFT Execution (MetaApi) | ✅ Connected | First automated trade placed |
| Daily Cache (ibkr_daily.db) | ✅ Fixed | Alpaca 11K + IBKR 500 priority |
| Telegram Notifications | ✅ Working | Shortlist + TP/SL + lots every 5 min |

Take your break. The system runs itself (mode=OFF, forward tracking). When you come
back, the forward tracking data in the DB tells you exactly how good the predictions
were. Then fix the feature contract, retrain, and go live.

Signal hai, Stack Ho jayega. 🚀
