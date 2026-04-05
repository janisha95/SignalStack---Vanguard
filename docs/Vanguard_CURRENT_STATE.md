# VANGUARD CURRENT STATE — Mar 31, 2026

## 1. What Vanguard Is

Vanguard is a multi-asset, multi-prop-firm, ML-driven intraday trading engine. It's the third system alongside Meridian (daily bars, US equities) and S1 (daily bars, strategy-based ML). Vanguard operates on 5-minute bars with a 5-minute execution cycle during market hours.

Target: 3 prop firm accounts running simultaneously:
- TTP (Trade The Pool) — US equities via SignalStack webhook
- FTMO — Forex/CFD/Crypto/Indices via MT5 API
- TopStep — Futures via Tradovate/NinjaTrader (future)

---

## 2. What's Built (325+ tests passing)

### Pipeline Stages

| Stage | File | Tests | Status | Description |
|---|---|---|---|---|
| V1 Cache | `stages/vanguard_cache.py` | 56 | ✅ DONE | 5m + 1h bars from Alpaca IEX. 247 symbols, 1.19M bars, 152.5 MB DB |
| V2 Prefilter | `stages/vanguard_prefilter.py` | 29 | ✅ DONE | Health checks: staleness, volume, spread, warm-up, session. 215/247 active |
| V3 Factor Engine | `stages/vanguard_factor_engine.py` | 101 | ✅ DONE | 35 features across 8 modules. 2.1% NaN rate |
| V4A Backfill | `stages/vanguard_training_backfill.py` | 50 | ✅ DONE | 38,132 rows, label_long 19.9%, label_short 23.6%. Session-aware TBM |
| V4B LGBM | `stages/vanguard_model_trainer.py` | 43 | ✅ DONE | lgbm_long AUC 0.6258, lgbm_short AUC 0.5706 |
| V5 Selection | `stages/vanguard_selection.py` | 26 | ✅ DONE | Strategy Router: regime gate, 13 strategies, ML gate, consensus count |
| V6 Risk | `stages/vanguard_risk_filters.py` | 35 | ✅ DONE | Multi-account risk: TTP/FTMO/TopStep, ATR sizing, sector/heat/corr caps |
| V7 Orchestrator | `stages/vanguard_orchestrator.py` | — | ❌ NOT BUILT | Session lifecycle 09:25-16:05 ET. Spec done |
| V7.1 Executor | `scripts/execute_daily_picks.py` | 66 | ✅ DONE | 7-tier signal architecture, SignalStack webhook, Telegram alerts |

### Helpers & Infrastructure

| Component | File | Tests | Status |
|---|---|---|---|
| Universe Builder | `vanguard/helpers/universe_builder.py` | 69 | ✅ DONE |
| SignalStack Adapter | `vanguard/execution/signalstack_adapter.py` | 18 | ✅ DONE |
| Telegram Alerts | `vanguard/execution/telegram_alerts.py` | 15 | ✅ DONE |
| Execution Bridge | `vanguard/execution/bridge.py` | — | ✅ DONE |
| Model Loader | `vanguard/models/model_loader.py` | — | ✅ DONE |
| Unified API | `vanguard/api/unified_api.py` | 88 | ✅ DONE |

### ML Models

| Model | Location | Data | Metrics |
|---|---|---|---|
| Vanguard LGBM Long | `models/vanguard/latest/lgbm_long.pkl` | 38K rows, 8 symbols | AUC 0.6258, IC +0.0213 |
| Vanguard LGBM Short | `models/vanguard/latest/lgbm_short.pkl` | 38K rows, 8 symbols | AUC 0.5706, IC +0.0292 |
| Vanguard TCN | — | FAILED (8 symbols too thin) | Retry after universe expansion |

### Config Files

| File | Contents |
|---|---|
| `config/vanguard_universes.json` | TTP (2K equities), FTMO (187 CFDs), TopStep (42 futures) |
| `.env` | SIGNALSTACK_WEBHOOK_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID |

---

## 3. What's NOT Built Yet

### V6 Enhancement: Pre-Execution Gate (future)

V6 currently sizes positions and approves/rejects against risk budgets. A pre-execution gate could add:
- Live price fetch + spread check before final approval
- Conviction threshold (require consensus_count ≥ 2 for live execution)
- Drawdown circuit-breaker (pause all accounts if combined loss exceeds threshold)

### V7 Orchestrator (spec complete, not implemented)

5-minute cycle loop: V1→V2→V3→V4B→V5→V6→Execute
Session lifecycle: 09:25 warm start → 09:35 first cycle → 15:25 last cycle → 15:50 EOD flatten → 16:05 close
Execution modes per account: off | paper | live

Spec: `VANGUARD_STAGE_V7_SPEC.md`

---

## 4. Unified UI (built but has bugs)

### Architecture
- React frontend at `~/SS/Meridian/ui/signalstack-app/` (port 3000)
- Unified backend API at `~/SS/Vanguard/vanguard/api/` (port 8090)
- Reads from Meridian DB (8080), S1 DB (8000), Vanguard DB

### Pages
| Page | Status | Features |
|---|---|---|
| `/candidates` | ⚠️ Working with bugs | Source selector (M/S1/V/Combined), Userviews, dynamic columns, filter/sort builders |
| `/trades` | ⚠️ Working with bugs | Trade Desk (order cards, risk sizing, signal tags), Trade Log |
| `/reports` | Built, not tested | Report config + Telegram send |
| `/` (Dashboard) | Unchanged | Original Meridian dashboard |
| `/model` | Unchanged | Shows MOCK/fallback status |

### Known UI Bugs
1. Meridian TCN shows 0.5 (fallback) instead of null — needs factor_history backfill
2. DELETE endpoint for execution_log missing — can't remove test trades
3. ATR stop calc uses hardcoded percentages, not real ATR
4. Userview restore incomplete (grouping field, filter/sort format mismatch)
5. S1 $0.00 prices on some tickers (no daily_bars fallback)

### Backend Contract
See: `BACKEND_CONTRACT.md` — source of truth for all API endpoints, field shapes, and rules

---

## 5. Required Backfills

### Meridian factor_history (BLOCKING TCN)
- 1,599,154 rows across 643 dates need 7 new columns filled
- Columns: momentum_impulse, volume_flow_direction, effort_vs_result, volatility_acceleration, wick_rejection (m1), rollover_strength (m3), rs_momentum (m5)
- Estimated: 10-30 hours
- Needs dedicated resumable script, run nohup overnight

### S1 Feature Slab from daily_bars
- Compute S1's 19-feature contract from Meridian daily_bars (11.1M rows)
- Then 3 materializations: RF labels, CNN sequences, scorer labels
- Enables S1 model retrain on 5 years of data

### Vanguard Universe Expansion
- Current: 247 symbols (IEX free tier limit after-hours)
- During market hours: 1,500-2,000 symbols available via SIP snapshots
- Re-run V1 backfill Monday during market hours
- Then retrain LGBM + attempt TCN with expanded data

---

## 6. Roadmap

### Week 1 (Mar 31 - Apr 4): Stabilize + Forward Validate
- [x] Execute trades on TTP Demo via Trade Desk
- [ ] Fix remaining UI bugs (contract audit findings)
- [ ] Run Meridian factor_history backfill overnight
- [ ] Expand Vanguard universe during market hours
- [ ] Retrain Vanguard LGBM with expanded universe
- [ ] Monitor TTP demo trades, update outcomes after 3-5 days

### Week 2 (Apr 7 - Apr 11): V5 Strategy Router ✅ COMPLETE
- [x] Build V5 equities strategies (SMC, Momentum, Mean Reversion, RR Edge, RS, Breakdown)
- [x] Build V5 regime gate
- [x] Build V5 consensus count
- [x] Test V5 with paper execution

### Week 3 (Apr 14 - Apr 18): V6 Risk Filters + V7 Orchestrator
- [x] Build V6 multi-account risk (TTP/FTMO/TopStep rules, ATR sizing, sector/corr caps)
- [ ] Build V7 session lifecycle (5-min cycle loop)
- [ ] Integration test: full Vanguard loop with paper execution
- [ ] Fund IBKR $500 for forex/futures data

### Week 4 (Apr 21 - Apr 25): Multi-Asset Expansion
- [ ] IBKR adapter for V1 (forex, futures data)
- [ ] MT5 adapter for V1 (FTMO CFDs)
- [ ] MT5 execution adapter for FTMO
- [ ] V5 forex/metals/crypto strategies active

### Week 5+ (Apr 28+): Production
- [ ] LLM strategy in V5 (optional overlay)
- [ ] Open TTP funded account ($50K-$100K)
- [ ] Open FTMO challenge
- [ ] Retire S1's agent_server.py (7,100+ lines)

---

## 7. Key File Paths

```
~/SS/Vanguard/                          # Vanguard root
├── data/vanguard_universe.db           # 152.5 MB, 1.19M bars
├── models/vanguard/latest/             # LGBM long + short
├── config/vanguard_universes.json      # TTP + FTMO + TopStep
├── .env                                # Webhook + Telegram tokens
├── stages/                             # V1-V7 pipeline stages
├── vanguard/
│   ├── api/                            # Unified API (port 8090)
│   │   ├── unified_api.py
│   │   ├── adapters/                   # Meridian, S1, Vanguard adapters
│   │   ├── field_registry.py
│   │   ├── userviews.py
│   │   ├── trade_desk.py
│   │   └── reports.py
│   ├── execution/                      # SignalStack + Telegram
│   ├── helpers/                        # DB, clock, universe builder
│   ├── factors/                        # V3 feature modules
│   └── strategies/                     # V5 strategy functions
├── scripts/
│   ├── execute_daily_picks.py          # 7-tier executor
│   └── run_single_cycle.py             # V1→V6 end-to-end test harness
└── tests/                              # 290+ tests
```

---

## 8. Server Ports

| Port | Service | Start Command |
|---|---|---|
| 8080 | Meridian API | `cd ~/SS/Meridian && python3 stages/v2_api_server.py` |
| 8000 | S1 API | `cd ~/SS/Advance && python3 agent_server.py` |
| 8090 | Unified API | `cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --port 8090` |
| 3000 | React Frontend | `cd ~/SS/Meridian/ui/signalstack-app && npm run dev` |
| — | Cloudflare Tunnel | `cloudflared tunnel --url localhost:8080` |
| — | All (1+2+5) | `~/SS/boot_signalstack.sh` |

---

## 9. Critical Rules (from lessons learned)

1. **Plan before building** — Write one complete spec before implementation. Never fragmented specs.
2. **Never modify production data without backups** — Prior incident deleted 157 rows permanently.
3. **v2_selection.py has DO NOT MODIFY warning** — Previous chat broke it catastrophically.
4. **Directional decoupling is a bug class** — `1 - tcn_score` ≠ "short probability". Need dedicated short models.
5. **Signal quality hierarchy** — 5-day swing >> intraday for current models. Intraday WR 32.1% below breakeven.
6. **Investigate before building** — On production systems, always investigate existing code first.
7. **Git backup before changes** — Commit working state before experimental changes.
8. **Execution defaults to OFF** — Forward tracking mode until validated.
