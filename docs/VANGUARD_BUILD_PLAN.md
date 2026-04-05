# PROJECT VANGUARD — BUILD PLAN

**Date:** Mar 29, 2026
**Author:** Shan + Claude Opus 4.6

---

## Build Order: Executor-First Strategy

### Why Executor First?

1. Meridian produces 30L + 30S daily picks NOW → no trades being placed
2. S1 produces validated ML picks NOW → no trades being placed
3. TTP $1M demo account sitting idle
4. Forward tracker resolves Apr 2 (passive) → real demo trades add execution validation
5. The executor is SYSTEM-AGNOSTIC — same code serves Meridian, S1, AND Vanguard
6. Placing 20-25 demo trades/day validates the full execution chain before Vanguard is ready

### The Executor Bridge

The executor takes a standardized trade object and sends it to SignalStack or MT5.
It doesn't care if the pick came from Meridian, S1, or Vanguard.

```python
# Universal trade object — same format from any system
trade = {
    "symbol": "AAPL",
    "direction": "LONG",           # or "SHORT"
    "shares": 100,
    "entry_price": 185.50,         # optional, market order if omitted
    "stop_price": 183.00,          # optional
    "tp_price": 190.00,            # optional
    "source_system": "meridian",   # or "s1", "vanguard"
    "account_id": "ttp_demo_1m",
    "execution_mode": "paper"      # or "live"
}
```

---

## Phase 0: Executor Bridge (This Week — Mon Mar 31 → Fri Apr 4)

### Goal
Place Meridian + S1 daily picks on TTP $1M demo via SignalStack.
20-25 trades per day. Forward-validate both systems with real execution.

### Build Order

**Day 1 (Mon Mar 31):**
- [ ] SignalStack adapter (`vanguard/execution/signalstack_adapter.py`)
  - HTTP POST to webhook URL
  - Payload: `{symbol, action, quantity}`
  - Actions: buy, sell, sell_short, buy_to_cover, close
  - Retry logic (2 retries, 1s delay)
  - Response logging
- [ ] Telegram alert module (`vanguard/execution/telegram_alerts.py`)
  - Send trade alerts, daily summaries, errors
- [ ] Execution bridge dispatcher (`vanguard/execution/bridge.py`)
  - Takes universal trade object → routes to correct adapter
  - Logs all executions to DB
- [ ] Create `vanguard_execution_log` table in vanguard_universe.db

**Day 2 (Tue Apr 1):**
- [ ] Meridian connector: script that reads Meridian's daily shortlist
  from v2_universe.db → converts to universal trade objects → feeds
  to executor bridge
- [ ] S1 connector: same for S1's picks
- [ ] Combined daily execution script:
  ```bash
  python3 execute_daily_picks.py --source meridian --account ttp_demo_1m
  python3 execute_daily_picks.py --source s1 --account ttp_demo_1m
  ```
- [ ] SignalStack account setup + webhook URL configured

**Day 3 (Wed Apr 2):**
- [ ] First live demo execution: Meridian + S1 picks placed on TTP demo
- [ ] Forward tracker resolves first batch (120 picks)
- [ ] Compare: passive forward tracking vs executed demo results
- [ ] Telegram daily summary report

**Day 4-5 (Thu-Fri Apr 3-4):**
- [ ] Iterate on execution: fix any SignalStack payload issues
- [ ] Add EOD flatten logic for intraday holds
- [ ] Monitor fill quality, latency, rejection reasons
- [ ] Tune position sizing for $1M demo account

### Files Created in Phase 0
```
~/SS/Vanguard/
├── vanguard/
│   └── execution/
│       ├── bridge.py                  # Dispatcher
│       ├── signalstack_adapter.py     # SignalStack HTTP client
│       ├── mt5_adapter.py             # MT5 stub (FTMO later)
│       └── telegram_alerts.py         # Telegram bot
├── scripts/
│   ├── execute_daily_picks.py         # Meridian/S1 → executor
│   └── eod_flatten.py                 # Close all positions
├── config/
│   ├── vanguard_accounts.json         # TTP demo account config
│   └── vanguard_execution_config.json # SignalStack URL, Telegram tokens
└── data/
    └── vanguard_universe.db           # execution_log table
```

---

## Phase 1: Vanguard Data Pipeline (Week of Apr 7)

### Build Order
- [ ] V1: vanguard_cache.py — Alpaca free data for US equities
  - 1m → 5m → 1h bar derivation
  - Equity prefilter: 12K → 1,500-2,000
  - Prefilter refresh: 9:35, 12:00, 3:00 ET
- [ ] V2: vanguard_prefilter.py — Live health monitor
  - 5 checks on survivors every 5 min
  - Session/stale/volume/spread/warm-up
- [ ] DB: vanguard_universe.db schema creation

### Dependencies
- Alpaca API key (already have)
- IBKR funded to $500 (Mon after funding) → subscribe to data bundles
  - Once IBKR ready: add IBKR adapter to V1

---

## Phase 2: Vanguard Features + ML (Week of Apr 14)

### Build Order
- [ ] V3: vanguard_factor_engine.py — 35 features, 7 modules
  - Install `smartmoneyconcepts` library for SMC features
  - 5m features + 1H HTF features
- [ ] V4A: vanguard_training_backfill.py — TBM labeling
  - label_long + label_short columns
  - Session-aware, no cross-session leakage
- [ ] V4B: vanguard_model_trainer.py — Track A: 2× LightGBM
  - lgbm_long + lgbm_short models
  - Walk-forward by session blocks
  - TCN dual-head (Track B) deferred to GPU availability

---

## Phase 3: Vanguard Selection + Risk (Week of Apr 21)

### Build Order
- [ ] V5: vanguard_selection.py — Strategy Router
  - 6 asset-class groups × 2-5 strategies each
  - Consensus counting
  - LLM strategy (stub, default OFF)
- [ ] V6: vanguard_risk_filters.py — Multi-account risk
  - TTP account config (real rules from platform inspection)
  - FTMO account config
  - Position sizing: shares/lots/contracts
  - EOD flatten logic

---

## Phase 4: Vanguard Orchestrator (Week of Apr 28)

### Build Order
- [ ] V7: vanguard_orchestrator.py — Session loop
  - 09:35-15:25 ET every 5 min
  - V1→V2→V3→V4B→V5→V6→Execute
  - Session lifecycle management
  - Failure handling, degraded mode
- [ ] Connect executor bridge (already built in Phase 0)
- [ ] Connect Telegram alerts (already built in Phase 0)
- [ ] Integration test: full cycle with paper execution

---

## Phase 5: IBKR + MT5 Data + FTMO (Week of May 5)

### Build Order
- [ ] IBKR adapter for V1 (US equities full SIP, forex, futures)
- [ ] MT5 adapter for V1 (FTMO-specific CFDs)
- [ ] MT5 execution adapter for FTMO account
- [ ] Multi-asset V5 strategies active (forex, metals, crypto, indices)

---

## Phase 6: LLM Strategy + Dashboard (Week of May 12)

### Build Order
- [ ] LLM strategy in V5 (news feed + economic calendar + Claude API)
- [ ] Dashboard integration into Meridian React app
  - Intraday panel alongside daily panel
  - Strategy view with consensus counts
  - Execution log view

---

## Summary Timeline

| Week | Phase | Deliverable |
|---|---|---|
| Mar 31 - Apr 4 | **Phase 0** | Executor bridge live on TTP demo with Meridian+S1 picks |
| Apr 7 - Apr 11 | Phase 1 | V1+V2 data pipeline running |
| Apr 14 - Apr 18 | Phase 2 | V3+V4A+V4B features + ML trained |
| Apr 21 - Apr 25 | Phase 3 | V5+V6 selection + risk filters |
| Apr 28 - May 2 | Phase 4 | V7 orchestrator, full Vanguard loop running |
| May 5 - May 9 | Phase 5 | IBKR + MT5 + FTMO multi-asset |
| May 12+ | Phase 6 | LLM strategy + dashboard |

---

## Critical Path

The ONLY blocker for Phase 0 is:
1. SignalStack account setup + webhook URL
2. TTP demo account credentials working

Everything else (Meridian picks, S1 picks, Telegram bot) already exists.

**Phase 0 can start Monday. No dependencies.**

---

## Risk Mitigation

| Risk | Mitigation |
|---|---|
| SignalStack webhook format wrong | Test with 1 share first, verify fill in TTP terminal |
| TTP demo rejects orders | Check order quantity limits, symbol availability |
| Too many trades overwhelm demo | Start with 10 trades Day 1, scale to 20-25 by Day 3 |
| Meridian picks not profitable | That's the point — forward validation tells us |
| IBKR funding delayed | Phase 0-3 don't need IBKR, use Alpaca free data |
