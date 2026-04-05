# SESSION 9 HANDOFF — Apr 1-2, 2026 (Mega Session)

## Systems Status

### Daily Swing (S1 + Meridian → TTP 20k) — OPERATIONAL
- S1: RF gate 0.55 validated (58.9% WR), NN disabled (0.90), scorer filters at 0.60L/0.50S
- Meridian: Dual TCN live (LONG IC +0.105, SHORT IC +0.392), bond ETFs blocked, factor_rank=0
- Trade pipeline: separate lanes (5+5 per system), writes to `trade_queue` in `signalstack_trades.db`
- Execution: TTP webhook (configured earlier), `ultra.sh` runs full pipeline

### Intraday (Vanguard) — E2E PROVEN, MODEL QUALITY ISSUE
- V4B scorer loads regressor ensembles (equity: LGBM+Ridge+ET, forex: RF+LGBM)
- V5 simple ranking replaces 13-strategy router
- Orchestrator has `--loop --interval 300` for continuous cycling
- **BLOCKER**: Forex regressors output near-constant predictions (all SHORT, ~-0.00005 range). Not a plumbing issue — models don't differentiate between pairs. Needs retraining or feature contract alignment investigation.
- Equity scoring works during market hours only (equities CLOSED after hours)

---

## Architecture Changes Made

### Meridian Stage 3/4/5 Rebuild
- **Stage 4**: Dual TCN — `tcn_pass_v1` (LONG) + `tcn_short_v1` (SHORT). LGBM killed entirely.
- **Stage 5**: Simple `nlargest()` rank by TCN score. No factor_rank, no beta stripping, no 60/40 blend.
- **Stage 2**: Bond/money-market ETF blocklist + inverse/leveraged ETFs + min ATR 0.5% filter
- **Root cause of stale ETFs**: `factor_matrix_daily` had same-day cached rows. Fixed to clear before rewrite.
- `factor_rank` = 0.0 (not 0.5), `predicted_return` = None (suppressed)

### S1 Gate System
- **RF gate**: Threshold 0.55 validated by forward tracking (58.9% WR at 0.55-0.60 tier)
- **NN gate**: Threshold raised to 0.90 (effectively disabled). Data shows FLAT — no signal at any threshold.
- **OR logic**: `allow = rf_pass or nn_pass` in `edge.py` line 141. With NN at 0.90, RF does all the work.
- **POST /config/nn_threshold is DEAD CODE** — edge.py reads `os.environ.get("NN_PROB_THRESHOLD")` directly
- **BRF strategy**: `gate_mode='off'` — bypasses RF gate entirely. Only short-indicating strategy. ML Scorer filters at 0.50.
- **Edge strategy**: Fixed `BUY` → `LONG` direction bug

### S1 Shortlist Pipeline (NEW)
```
Evening scan → RF gate (0.55) → PASS rows → ML Scorer (LightGBM)
→ LONG: scorer >= 0.60, SHORT: scorer >= 0.50 → top 5 per side
→ Writes to daily_shortlist table in signalstack_results.db
```

### Vanguard V4B/V5 Rewrite
- **V4B** (`vanguard_scorer.py`): Loads pre-trained .pkl regressors, ensemble predict → `vanguard_predictions`
- **V5** (`vanguard_selection_simple.py`): Rank by |predicted_return|, store 30+30, show 5+5 → `vanguard_shortlist_v2`
- Old V4B classifier trainer and V5 13-strategy router are superseded (files still exist but not called)

### Reports Removed
- `s1_pass_scorer.py`: 321 lines of Telegram/Haiku report code removed. Scoring logic kept.
- `s1_morning_report_v2.py`: Stubbed (12 lines)
- `s1_evening_report_v2.py`: Stubbed (11 lines)
- `s1_convergence_pipeline.py`: Haiku/Telegram removed
- `s1_night_expansion.py` + `s1_evening_orchestrator.py`: Telegram removed

---

## Gate Audit Results (CRITICAL REFERENCE)

### RF Gate
- Function: `_run_edge_ml_gate` in `modules/strategies/edge.py` lines 139-151
- Threshold source: `_GATE_CONFIG` dict (registry defaults + DB overrides via `_load_gate_config_from_db`)
- DB wins over registry at runtime for gate_mode
- Forward tracking: 0.55-0.60 tier = 58.9% WR on 185 candidates (1,325 deduped, yfinance-resolved)

### NN Gate  
- Threshold: `NN_THRESHOLD = float(os.environ.get("NN_PROB_THRESHOLD", "0.30"))` in edge.py
- Set to 0.90 in `~/.zshrc` and `~/SS/.env.shared`
- Forward tracking: FLAT across all tiers (27-50% WR)
- `_nn_threshold_runtime` dict exists but edge.py doesn't read it — dead code

### Gate Config DB State
```
strategy | gate_mode | ml_threshold | strategy_threshold
edge     | strategy  | 0.575        | 0.55
rct      | strategy  | 0.51         | 0.50
mr       | ml        | 0.53         | 0.45
brf      | off       | NULL         | 0.45    ← bypasses gate
wyckoff  | ml        | NULL         | 0.50    ← falls back to registry 0.40
spectre  | ml        | NULL         | 0.50
srs      | ml        | 0.45         | 0.45    ← disabled strategy
```

### ML Scorer Audit
- LightGBM classifier, 19 CORE_FEATURES, 131K training samples, 65 WF windows
- avg_auc = 0.548, avg_top_decile_wr = 0.603
- Top features: vix_level, atr_pct, relative_strength, days_since_52w_high
- Scorer is gate-agnostic (avg ~0.44 regardless of gate_source)
- Only 109 rows have both RF>=0.55 AND Scorer>=0.60 (2% overlap)

---

## Model Registry

### Meridian (Daily, US Equities)
| Model | Path | IC | Features |
|---|---|---|---|
| TCN Long v1 | `~/SS/Meridian/models/tcn_pass_v1/` | +0.105 | 19 |
| TCN Short v1 | `~/SS/Meridian/models/tcn_short_v1/` | +0.392 | 19 |

### S1 (Daily, US Equities)
| Model | Metric | Notes |
|---|---|---|
| RF Gate | WR 58.9% at 0.55+ | Inline in agent_server.py |
| NN Gate | FLAT | Disabled at 0.90 |
| ML Scorer | AUC 0.548, top-decile WR 60.3% | `~/SS/Advance/models/pass_scorer.pkl` |

### Vanguard Intraday Regressors
| Model | Path | IC | Features | Asset |
|---|---|---|---|---|
| LGBM Equity | `models/equity_regressors/lgbm_equity_regressor_v1.pkl` | +0.034 | 29 | equity |
| Ridge Equity | `models/equity_regressors/ridge_equity_regressor_v1.pkl` | +0.025 | 29 | equity |
| ET Equity | `models/equity_regressors/et_equity_regressor_v1.pkl` | +0.025 | 29 | equity |
| RF Forex | `models/forex_regressors/rf_forex_regressor_v1.pkl` | +0.049 | 27 | forex |
| LGBM Forex | `models/forex_regressors/lgbm_forex_regressor_v1.pkl` | +0.037 | 27 | forex |

Ensemble weights: Equity = 0.4 LGBM + 0.3 Ridge + 0.3 ET | Forex = 0.5 RF + 0.5 LGBM

### Vanguard Intraday Classifiers (available but NOT used in production)
| Model | Path | AUC | Notes |
|---|---|---|---|
| LGBM Equity Long | `models/equity_classifiers/lgbm_equity_long_v1.pkl` | 0.611 | 23 features |
| LGBM Equity Short | `models/equity_classifiers/lgbm_equity_short_v1.pkl` | 0.632 | 35 features |
| LGBM Forex Long | `models/forex_classifiers/lgbm_forex_long_v1.pkl` | 0.559 | 23 features |
| LGBM Forex Short | `models/forex_classifiers/lgbm_forex_short_v1.pkl` | 0.539 | 23 features |

---

## DB Map

| DB | Path | Size | Key Tables |
|---|---|---|---|
| S1 Standings | `~/SS/Advance/data_cache/s1_standings.db` | 62MB | standings |
| S1 Results | `~/SS/Advance/data_cache/signalstack_results.db` | 3MB | evening_pass, scorer_predictions, convergence_picks, daily_shortlist |
| S1 Forward Tracking | `~/SS/Advance/data_cache/s1_forward_tracking.db` | ~5MB | candidate_journal, outcome_journal |
| S1 Metrics | `~/SS/Advance/signalstack_metrics.db` | ~1MB | gate_config |
| Meridian | `~/SS/Meridian/data/v2_universe.db` | 3.2GB | prefilter_results, factor_matrix_daily, predictions_daily, shortlist_daily, meridian_shortlist_final |
| Vanguard | `~/SS/Vanguard/data/vanguard_universe.db` | 10.2GB | vanguard_bars_1m, vanguard_bars_5m, vanguard_features, vanguard_factor_matrix, universe_members, vanguard_predictions, vanguard_shortlist_v2, account_profiles |
| Trade Queue | `~/SS/data/signalstack_trades.db` | <1MB | trade_queue |

---

## Env Vars

```
NN_PROB_THRESHOLD=0.90          # ~/.zshrc + ~/SS/.env.shared
TWELVE_DATA_API_KEY=<key>       # ~/.zshrc + ~/SS/.env.shared + ~/SS/Vanguard/.env
```

---

## Known Issues

### P0
1. **Vanguard forex predictions near-constant** — all 35 SHORT, ~-0.00005 range. NaN features fixed but model quality issue remains. Regressors may need retraining on aligned feature set.
2. **Meridian longs still have equity ETFs** (SCHD, GFLW, DFAU, NOBL) — bond ETFs gone but equity ETFs pass the 0.5% ATR filter. May need instrument_type filter.
3. **Vanguard DB still 10GB** — purged 1m bars but growing. Split into multiple DBs or PostgreSQL migration needed.

### P1
4. **IBKR data not wired** — IB Gateway running, `ib_insync` not installed, no adapter written
5. **MT5 execution not automated** — manual on web terminal for now. Docker MT5 on Mac or MetaApi cloud for automation.
6. **boot_signalstack.sh and health_check.sh** still reference port 8001 (should be 8008)
7. **S1 registry defaults** still have old thresholds (wyckoff 0.40). DB overrides are correct but should match.
8. **Forward tracking** needs restart for Meridian (new architecture). S1 data clean (Mar 24 purged).

### P2
9. Crypto/metal models fail (14 crypto symbols, 3 metal — insufficient data)
10. MCP market brief tool needed (replace Telegram reports)
11. Forward tracking DB unification (3 DBs → 1-2)
12. S1 NN gate: remove from code entirely or retrain

---

## Prop Firm Accounts

| Account | Platform | Budget | System | Status |
|---|---|---|---|---|
| TTP 20k Swing | TTP | $450 challenge | S1 + Meridian daily | READY for Monday |
| GFT 5k Normal | MT5 | $5,000 | Vanguard forex | MODEL QUALITY ISSUE |
| GFT 10k Pay Later | MT5 | $10,000 (FREE) | Vanguard forex | MODEL QUALITY ISSUE |
| TTP 50k Intraday | TTP | TBD | Vanguard equity | Needs market hours test |

---

## Key Commits This Session

### Meridian
- `19f7cd8` — Stage 3/4/5 dual TCN rebuild
- `2e9c491` — Bond ETF blocklist + min vol filter
- `86ec68d` — Prefilter enforcement fix (clear stale factor_matrix rows)
- `f910d12` — meridian_daily_shortlist.py

### Advance (S1)
- `603b4a5` — Report code removal (7 files)
- `01230cd` — Edge BUY→LONG, BRF gate bypass, Mar 24 purge
- `483511f` — S1 daily shortlist pipeline + NN threshold 0.90
- `863b912` — Separate lane allocation in trade pipeline

### Vanguard
- `3fa93df` — V4B regressor scorer + V5 simple selection
- `f5e3948` — session_phase dtype fix
- `ef06bad` — NaN features fallback, API key loading, loop mode

---

## Critical Rules

1. Investigate existing code before modifying
2. No specs without consent — discuss design first
3. Git backup before changes
4. Trust the user on bugs — investigate, don't defend
5. RF gate 0.55 is validated (58.9% WR)
6. NN gate is useless — data shows no signal
7. Regressor >> Classifier for intraday 5m bars
8. UTC in DB, ET for display
9. Don't use sed for production fixes — use CC/Codex
10. Session handoff via Notion — read START HERE index first
