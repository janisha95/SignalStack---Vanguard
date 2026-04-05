# Vanguard Intraday System — Current Architecture (Updated Apr 2, 2026 4:30 PM ET)

## Status: OPERATIONAL — Equity + Forex picks flowing every 5 min

Cycle time: 20s steady-state (V1=1.97s). Telegram notifications active.
Latest picks: Equity 30L+28S, Forex 7L+7S. V6 approving 58-72 per account.

## Pipeline (after all Apr 2 fixes)

```
V1 Data (1.97s steady-state)
├── IBKR Streaming: forex 14 pairs via keepUpToDate=True (FREE IDEALPRO)
├── Alpaca WS: equity ~291 symbols (FREE IEX)
├── Twelve Data: crypto 14 symbols only (forex REMOVED from TD)
└── Aggregator: 1m→5m→1h

V2 Health (0.84s) → 51 ACTIVE survivors (equity + forex + crypto)
V3 Features (0.55s) → 35 features per symbol (equity=77, forex=14, crypto=7)

V4B Scorer (1.39s)
├── Cross-sectional standardization BEFORE predict() ← GROK FIX
├── Equity: 0.4×LGBM(IC+0.034) + 0.3×Ridge(IC+0.025) + 0.3×ET(IC+0.025)
├── Forex: 0.5×RF(IC+0.049) + 0.5×LGBM(IC+0.037)
└── Output: predicted_return (float)

V5 Selection (0.01s)
├── Cross-sectional percentile ranking ← GROK FIX
├── edge_score = rank(predicted_return, pct=True) * 0.98 + 0.01
├── LONG% = ~44% (was 0% before fix)
└── Top 30L + 30S per asset class

V6 Risk (0.33s) → TTP 58 approved, FTMO 72 approved (relaxed for testing)
V7 Execute → mode=OFF, forward-tracking + Telegram notifications
```

## Data Sources

| Source | Assets | Method | Cost |
|---|---|---|---|
| IBKR IDEALPRO | 14 forex pairs | Streaming (keepUpToDate) | FREE |
| IBKR PAXOS | BTC/ETH/LTC/BCH | Streaming | FREE |
| Alpaca | ~291 US equities | WebSocket real-time | FREE (IEX) |
| Twelve Data | 14 crypto | REST polling | FREE tier |

## Databases

| DB | Path | Size | Consumers |
|---|---|---|---|
| vanguard_universe.db | ~/SS/Vanguard/data/ | 10.2GB | Vanguard V1-V7 |
| ibkr_intraday.db | ~/SS/Vanguard/data/ | ~50MB | IBKR streaming bars |
| ibkr_daily.db | ~/SS/Meridian/data/ | ~1GB | S1 + Meridian (shared, 11.9K tickers) |
| v2_universe.db | ~/SS/Meridian/data/ | 3.2GB | Meridian stages |

## Models

### Deployed (V4B)
| Model | Asset | IC | Features | Weight |
|---|---|---|---|---|
| LGBM equity | equity | +0.034 | 29 | 0.4 |
| Ridge equity | equity | +0.025 | 29 | 0.3 |
| ET equity | equity | +0.025 | 29 | 0.3 |
| RF forex | forex | +0.049 | 27 | 0.5 |
| LGBM forex | forex | +0.037 | 27 | 0.5 |

### Missing (need training)
- metal/energy: XAUUSD, USOIL etc. (CC prompt written)
- index: US500, US100 etc. (CC prompt written)
- crypto: BTC, ETH etc. (V3 features exist but no scorer model)

## 35 V3 Features (10 modules)

Price Location: session_vwap_distance, premium_discount_zone, gap_pct, session_opening_range_position
Momentum: momentum_3bar, momentum_12bar, momentum_acceleration, atr_expansion
Volume: relative_volume, volume_burst_z, down_volume_ratio, effort_vs_result
Rel Strength: rs_vs_benchmark_intraday, daily_rs_vs_benchmark, benchmark_momentum_12bar, cross_asset_correlation
Session: session_phase, time_in_session_pct, bars_since_open
Daily Bridge: daily_adx, daily_drawdown_from_high, daily_conviction
SMC 5m: ob_proximity_5m, fvg_bullish_nearest, fvg_bearish_nearest, structure_break, liquidity_sweep, smc_premium_discount
HTF 1h: htf_trend_direction, htf_structure_break, htf_fvg_nearest, htf_ob_proximity

Equity model uses 29 features. Forex model uses 27 (excludes relative_volume, effort_vs_result).

## Key Fixes Applied Today (Apr 2)

1. Cross-sectional percentile ranking in V5 (edge_score 0.02-0.99, LONG% 44%)
2. Feature standardization before predict() in V4B
3. V6 readiness gate: uses edge_score, not old classifier ml_prob
4. Sector cap: unknown equities no longer default to "technology"
5. IBKR streaming for forex (replaces Twelve Data, eliminates 62s bottleneck)
6. ETF exclusion from Meridian LONG selection (797 ETFs via yfinance)
7. Telegram shortlist notifications with edge_score
8. Alpaca key loading from .env files
9. Forex pipeline: V2 warmup floor lowered, IBKR seeds 5h history

## Known Remaining Issues

- IBKR equity streaming not enabled (using Alpaca fallback)
- IEX data is delayed for some stocks ($3/mo NASDAQ+NYSE fixes this)
- No metal/energy/index/crypto models
- V6 rules relaxed for testing (need proper per-account rules)
- Train-serving skew: V4A and V3 are different code paths (need shared feature module)
- No MT5 executor for GFT forex (signals flow but can't execute)
- No Vanguard UI/frontend

## File Map

```
~/SS/
├── Advance/                           # S1 daily system
│   ├── agent_server.py                # Main server (~10,600 lines)
│   ├── trading_dashboard.html         # UI (~9,433 lines)
│   ├── s1_evening_orchestrator.py     # Evening scan
│   ├── s1_daily_shortlist.py          # Top 5 picks
│   └── modules/strategies/            # BRF, RCT, Wyckoff, MR, SRS
│
├── Meridian/                          # Daily swing system
│   ├── stages/v2_*.py                 # Stages 1-7
│   ├── models/tcn_*/ridge_*/lgbm_*/   # TCN + LGBM + Ridge models
│   ├── config/etf_tickers.json        # 797 ETFs excluded from LONG
│   └── data/ibkr_daily.db            # Shared daily bars (S1 + Meridian)
│
├── Vanguard/                          # Intraday system
│   ├── stages/
│   │   ├── vanguard_orchestrator.py   # V7: main loop + Telegram
│   │   ├── vanguard_prefilter.py      # V2: health checks
│   │   ├── vanguard_factor_engine.py  # V3: 35 features
│   │   ├── vanguard_scorer.py         # V4B: ensemble scoring
│   │   ├── vanguard_selection_simple.py # V5: cross-sectional ranking
│   │   ├── vanguard_risk_filters.py   # V6: risk gates
│   │   └── vanguard_training_backfill.py # V4A: training data gen
│   ├── vanguard/data_adapters/
│   │   ├── alpaca_adapter.py          # Equity WS + REST
│   │   ├── ibkr_adapter.py            # IBKR streaming + daily
│   │   └── twelvedata_adapter.py      # Crypto REST only
│   ├── vanguard/helpers/
│   │   ├── universe_builder.py        # Canonical universe
│   │   ├── bars.py                    # Aggregation
│   │   └── clock.py                   # Session/timezone
│   ├── models/equity_regressors/      # LGBM + Ridge + ET
│   ├── models/forex_regressors/       # RF + LGBM
│   └── data/vanguard_universe.db      # Main DB (10.2GB)
│
├── signalstack_mcp_server.py          # MCP v3 (10 tools)
├── signalstack_trade_pipeline.py      # Daily trade queue
├── ultra.sh                           # Master runner
└── .env                               # API keys
```

## Prop Firm Accounts

| Account | Platform | System | Signals | Executor | Monday Ready? |
|---|---|---|---|---|---|
| TTP 20K Swing | Trader Evo | S1+Meridian | ✅ 15 picks | ❌ Manual | YES (manual) |
| GFT 5K Normal | MT5 | Vanguard forex | ✅ 7L+7S | ❌ Need MT5 API | NO |
| GFT 10K Pay Later | MT5 | Vanguard forex | ✅ 7L+7S | ❌ Need MT5 API | NO |
| TTP 50K Intraday | Trader Evo | Vanguard equity | ✅ 30L+28S | ❌ Need webhook | PARTIAL |
