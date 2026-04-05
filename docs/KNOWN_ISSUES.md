# KNOWN ISSUES — Updated Apr 2, 2026

## P0 — Blocking Production

### Vanguard Forex Predictions Near-Constant
- **Symptom**: All 35 forex predictions are SHORT with range -0.00005 to -0.000008. No differentiation between pairs.
- **Root cause**: NOT plumbing (NaN features fixed, API key fixed, orchestrator runs clean). The forex regressors (RF IC 0.049, LGBM IC 0.037) produce near-identical outputs in production. Possible causes: (a) feature contract mismatch between training and production V3, (b) models trained on different feature scale/normalization, (c) production features are all similar values after NaN→0 conversion.
- **Investigation needed**: Compare feature distributions in training data vs live V3 output. Check if V3 features match the feature list in `forex_regressors/meta.json`.
- **Files**: `~/SS/Vanguard/stages/vanguard_scorer.py`, `~/SS/Vanguard/models/forex_regressors/`

### Meridian Longs Still ETF-Heavy
- **Symptom**: Top 5 longs are SCHD, CNS, GFLW, DFAU, NOBL — equity ETFs, not individual stocks
- **Root cause**: Bond ETFs are blocked but equity ETFs (dividend, all-cap) pass the 0.5% ATR filter
- **Fix options**: (a) Add equity ETF tickers to blocklist, (b) add `instrument_type != 'ETF'` filter if data available, (c) raise min ATR threshold
- **Files**: `~/SS/Meridian/stages/v2_prefilter.py`

## P1 — Important

### S1 NN Gate POST Endpoint is Dead Code
- **Symptom**: `POST /config/nn_threshold` updates `_nn_threshold_runtime` but `edge.py` reads `os.environ.get("NN_PROB_THRESHOLD")` directly
- **Impact**: UI/API threshold changes have no effect on gate behavior
- **Fix**: Either wire `edge.py` to read from `_nn_threshold_runtime`, or remove the endpoint
- **Files**: `~/SS/Advance/agent_server.py` (line 2551+), `~/SS/Advance/modules/strategies/edge.py`

### S1 Registry vs DB Threshold Mismatch
- **Symptom**: Registry has wyckoff threshold=0.40, DB has NULL (falls back to 0.40). Should be 0.55.
- **Impact**: Wyckoff candidates pass at 0.40 instead of 0.55. Mitigated by NN being disabled, but RF alone at 0.40 lets noise through.
- **Fix**: Update registry defaults + DB values for all strategies to ml_threshold=0.55
- **Files**: `~/SS/Advance/signalstack_strategy_registry.py`, `~/SS/Advance/signalstack_metrics.db`

### BRF Gate Mode = 'off'
- **Symptom**: BRF bypasses RF gate entirely. All BRF candidates get PASS regardless of p_tp.
- **Impact**: Intentional — BRF is the only short strategy, ML Scorer filters at 0.50. But means RF validation (58.9% WR) doesn't apply to BRF shorts.
- **Risk**: If scorer quality degrades, garbage shorts enter the shortlist
- **Decision**: Accepted for now. Monitor scorer WR on BRF shorts.

### Vanguard DB Size (10.2GB)
- **Symptom**: SQLite DB at 10GB after purging 1m bars. Crashes with file descriptor leaks on heavy V3 processing.
- **Fix**: Split into multiple DBs (bars, features, predictions) or migrate to PostgreSQL
- **Workaround**: Periodic purge of old 1m bars (`DELETE FROM vanguard_bars_1m WHERE bar_ts_utc < datetime('now', '-7 days'); VACUUM;`)

### boot_signalstack.sh / health_check.sh Port Mismatch
- **Symptom**: Both reference port 8001 for S8 Model Factory. Should be 8008 for S8 Cache Server.
- **Fix**: sed replace 8001→8008 in both files
- **Files**: `~/SS/boot_signalstack.sh`, `~/SS/health_check.sh`

## P2 — Next Week

### Crypto/Metal Models Fail
- 14 crypto symbols, 3 metal symbols — insufficient training data
- Need Twelve Data historical backfill (12+ months)
- Crypto IC: -0.002 (FAIL), Metal IC: negative (FAIL)

### Forward Tracking Gaps
- Meridian: No forward tracking since architecture change. Needs restart.
- S1: Clean data (Mar 24 purged). Captures from Mar 23, 25, 28, 31.
- Vanguard: No forward tracking exists yet.
- Need unified forward tracking across all 3 systems (currently 3 separate DBs)

### MCP Market Brief
- Removed Telegram reports (Haiku/RSS). Need MCP replacement.
- Should provide: VIX level, SPY trend, sector rotation, news headlines for shortlist tickers
- Replaces the old `s1_pass_scorer.py` evening report and `s1_morning_report_v2.py`

### IBKR Integration
- Funded with $1K. IB Gateway running on Mac.
- Need: `ib_insync` install, adapter script, data subscription ($15/mo)
- Provides: better equity data than Alpaca, real execution path, withdrawable profits

### MT5 Execution Automation
- Current: manual on MT5 Web Terminal
- Options: Docker MT5 on Mac, MetaApi cloud ($15-50/mo), SignalStack webhook (100 free/mo)
- GFT only provides MT5 platform (no REST API, no webhook endpoint)
