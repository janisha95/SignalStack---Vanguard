# STAGE V1: Vanguard Data Pipeline — vanguard_cache.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 28, 2026
**Covers:** US Equities (TTP) + Forex + CFDs (FTMO) + Futures (Topstep/Apex)

---

## What Stage V1 Does

Stage V1 is a long-running multi-asset data service that:

1. On startup, pulls the full Alpaca equity list (~12K), applies a fast prefilter to select ~1,500-2,000 tradeable equities, then subscribes to 1m bar streaming for survivors only
2. Refreshes the equity prefilter at 9:35 AM, 12:00 PM, and 3:00 PM ET
3. Streams 1m bars for 187 pre-curated FTMO instruments via MT5
4. Persists all bars to vanguard_universe.db
5. Derives 5m and 1h bars from stored 1m bars
6. Runs live health monitoring every 5 minutes between prefilter refreshes
7. Maintains heartbeat metadata for downstream stages

V1 owns ALL data intake, bar integrity, bar aggregation, AND the equity prefilter.

---

## Architecture

```
                    vanguard_cache.py
  ┌─────────────────────┐   ┌──────────────────────┐
  │  Equity Prefilter    │   │  Universe Loader      │
  │  12K → 1,500-2,000   │   │  187 FTMO instruments │
  │  Runs: 9:35/12/3     │   │  From JSON config     │
  └──────────┬──────────┘   └──────────┬───────────┘
             │                          │
  ┌──────────▼──────────┐   ┌──────────▼───────────┐
  │  Alpaca Adapter      │   │  MT5 Adapter          │
  │  WebSocket 1m bars   │   │  Poll 1m bars / 30s   │
  │  US equities only    │   │  Forex/CFD/Crypto     │
  └──────────┬──────────┘   └──────────┬───────────┘
             └──────────┬───────────────┘
                        │
             ┌──────────▼───────────┐
             │  Bar Aggregator       │
             │  1m → 5m → 1h        │
             └──────────┬───────────┘
                        │
             ┌──────────▼───────────┐
             │  vanguard_universe.db │
             └──────────────────────┘

Future (Monday+): IBKR Adapter replaces Alpaca (full SIP $14.50/mo)
```

---

## 1. Data Adapters

### 1A. Alpaca Adapter (Now — Free)
- Purpose: US equities 1m bars for TTP lane
- Method: WebSocket streaming (real-time) + REST API (historical backfill)
- Coverage: All US equities on IEX
- Cost: $0/mo
- Limitation: IEX only (~2% volume). IBKR upgrade Monday provides full SIP.

### 1B. IBKR Adapter (Monday — $14.50/mo)
- Purpose: Replaces Alpaca for equities + adds forex + adds futures
- Method: TWS API via ib_async, reqHistoricalData with keepUpToDate=True
- Coverage: Full SIP US equities + IDEALPRO forex + CME/CBOT/COMEX/NYMEX futures
- Requires: $500 equity in IBKR account, IB Gateway running locally

### 1C. MT5 Adapter (Now — Free)
- Purpose: FTMO-specific CFD instruments
- Method: Poll via MetaTrader5 Python library every 30 seconds
- Coverage: All 187 FTMO instruments
- CRITICAL: MT5 time field is bar OPEN time. Must add 60s to convert to bar END time.

---

## 2. Equity Prefilter (Inside V1)

### Schedule
| Time (ET) | Action |
|---|---|
| 9:15 AM | Startup prefilter (previous day daily bars) |
| 9:35 AM | First refresh (first 5 min of intraday data) |
| 12:00 PM | Midday refresh |
| 3:00 PM | Afternoon refresh |

### Filter Rules

| Filter | Threshold | Rationale |
|---|---|---|
| Price floor | Close > $2.00 | Penny stocks too volatile |
| Dollar volume (opening) | > $1M avg daily | Need liquidity |
| Dollar volume (rolling) | > $500K intraday | Catch volume collapse |
| Suffix exclusion | No .WS, .WT, .U, .R | Warrants/units/rights |
| Leveraged/Inverse ETF | Exclude known list | TQQQ, SQQQ, UVXY, etc. |
| Min daily bars | >= 20 | Need history for context |
| TTP: Avg daily volume | > 500K shares | TTP overnight requirement |
| TTP: No 8%+ in 4 min | Check intraday bars | TTP circuit breaker |
| TTP: No earnings today | Check earnings calendar | TTP earnings-day ban |
| TTP: Not halted | Check Alpaca status | TTP halted stock ban |

### Performance Target
Equity prefilter MUST complete in under 30 seconds.
Method: SQL aggregates on daily data ONLY. No full OHLCV load for 12K tickers.

### Prop Firm Config
Rules loaded from config/vanguard_prop_firm_rules.json with presets for:
- ttp_intraday (price $2, vol $1M, earnings block, halt block, 8% rule)
- ttp_swing (price $1, vol $500K, earnings block, halt block)
- ftmo (standard FTMO rules)
- topstep (trailing drawdown rules)

### Expected Survivor Counts
| Time | Equity | FTMO | Total |
|---|---|---|---|
| 9:35 AM | ~1,500-2,000 | ~170-180 | ~1,670-2,180 |
| 12:00 PM | ~1,200-1,800 | ~170-180 | ~1,370-1,980 |
| 3:00 PM | ~1,000-1,500 | ~130-140 | ~1,130-1,640 |
| Weekend | 0 | ~20-25 crypto | ~20-25 |

---

## 3. Live Health Monitoring (Every 5 min between refreshes)

| Check | Condition | Action |
|---|---|---|
| Stale data | No new 1m bar > 10 min | Mark STALE |
| Halted | Alpaca trading_status != active | Mark HALTED |
| Spread blowout | Spread > 3x average | Mark WIDE_SPREAD |
| Volume collapse | Last 3 5m bars tick_vol < 5 | Mark LOW_VOLUME |

---

## 4. Bar Storage

### vanguard_bars_1m
symbol, asset_class, path, ts_utc (bar END, UTC), OHLCV, tick_volume, spread, source, ingest_ts_utc
PK: (symbol, ts_utc)

### vanguard_bars_5m
Same schema + bar_count (should be 5, <5 = partial) + spread_avg
Derived from stored 1m bars, clock-aligned (00, 05, 10, ...)

### vanguard_bars_1h
Same schema + bar_count (should be 60, <60 = partial)
Derived from stored 1m bars, clock-aligned

### vanguard_prefilter_results
cycle_ts_utc, symbol, asset_class, path, status, latest_close, avg_dollar_volume, avg_daily_volume, bars_in_db, source
PK: (cycle_ts_utc, symbol)

### vanguard_cache_meta
Standard key-value for service state, heartbeat, counts, timestamps

---

## 5. Timestamp Rules
- All stored as UTC ISO-8601
- ts_utc = bar END time (not open)
- Alpaca/MT5/IBKR all return bar OPEN time → add interval to convert
- Session logic uses America/New_York (crypto uses UTC)

---

## 6. Session Awareness
- Equities: 9:30 AM - 4:00 PM ET (Mon-Fri)
- Forex: Sun 5 PM - Fri 5 PM ET (24/5)
- CFDs: Per-asset-class from config
- Futures: Sun 6 PM - Fri 5 PM ET with daily break
- Crypto: 24/7 (30-min cadence on weekends)

---

## 7. Polling Cadence
| Path | Method | Cadence |
|---|---|---|
| Equities (Alpaca) | WebSocket | Real-time |
| Equities (IBKR) | keepUpToDate=True | Real-time |
| Forex/CFDs (MT5) | copy_rates_from_pos | Every 30 seconds |
| Futures (IBKR) | keepUpToDate=True | Real-time |
| Crypto weekend | MT5 polling | Every 30 minutes |

---

## 8. File Structure
```
~/SS/Meridian/
├── stages/vanguard_cache.py
├── vanguard/
│   ├── adapters/
│   │   ├── alpaca_adapter.py
│   │   ├── ibkr_adapter.py
│   │   └── mt5_adapter.py
│   ├── helpers/
│   │   ├── equity_prefilter.py
│   │   ├── bar_aggregator.py
│   │   ├── session_manager.py
│   │   ├── health_monitor.py
│   │   ├── db.py
│   │   └── clock.py
│   └── __init__.py
├── config/
│   ├── vanguard_universe.json
│   ├── vanguard_mt5_symbol_map.json
│   ├── vanguard_prop_firm_rules.json
│   ├── vanguard_leveraged_etfs.json
│   └── vanguard_config.json
├── data/vanguard_universe.db
└── tests/test_vanguard_cache.py
```

---

## 9. CLI
```bash
python3 stages/vanguard_cache.py                    # Normal operation
python3 stages/vanguard_cache.py --dry-run           # Validate, no streaming
python3 stages/vanguard_cache.py --backfill-days 5   # Historical download
python3 stages/vanguard_cache.py --path equity       # Equity only
python3 stages/vanguard_cache.py --path ftmo         # FTMO only
python3 stages/vanguard_cache.py --symbols AAPL,NVDA # Specific symbols
python3 stages/vanguard_cache.py --adapter ibkr      # Use IBKR instead of Alpaca
python3 stages/vanguard_cache.py --rebuild-5m        # Rebuild derived bars
python3 stages/vanguard_cache.py --force-prefilter   # Force prefilter refresh
```

---

## 10. DB Isolation
- Writes ONLY to vanguard_universe.db
- MUST NOT read/write Meridian DB (v2_universe.db)
- Startup guard: assert 'v2_universe' not in db_path
- PRAGMA: WAL mode, busy_timeout=30000, synchronous=NORMAL

---

## 11. Failure Handling

### Hard Fail
- Alpaca API keys missing/invalid
- DB path points to Meridian DB
- Config files missing
- Zero equity survivors after prefilter

### Soft Recoverable
- MT5 not running → equity path still works
- WebSocket disconnect → auto-reconnect
- MT5 disconnect → reconnect with backoff
- Individual symbol stale → skip, mark stale

### Escalation (Telegram)
- No heartbeat > 5 min during ACTIVE
- Reconnect fails 5 times
- > 30% survivors go stale
- Zero survivors at refresh
- DB write errors

---

## 12. Performance Targets
| Operation | Target | Red Flag |
|---|---|---|
| Equity prefilter (12K → 1.5-2K) | < 30 sec | > 60 sec |
| MT5 poll cycle (187 symbols) | < 2 sec | > 5 sec |
| 5m aggregation cycle | < 1 sec | > 3 sec |
| Live health check | < 5 sec | > 10 sec |
| DB size per day | ~50-100 MB | > 500 MB |
| Memory usage | < 500 MB | > 1 GB |

---

## 13. Acceptance Criteria
- [ ] stages/vanguard_cache.py exists and runs
- [ ] Alpaca adapter streams 1m bars for equity survivors
- [ ] MT5 adapter polls 1m bars for FTMO instruments
- [ ] Equity prefilter: 12K → 1,500-2,000 in < 30 seconds
- [ ] Prefilter refreshes at 9:35, 12:00, 3:00 ET
- [ ] TTP rules applied (earnings, halts, volume, circuit breaker)
- [ ] Live health monitoring every 5 min
- [ ] 5m and 1h bars derived from 1m
- [ ] UTC bar END time convention
- [ ] Multi-asset session awareness
- [ ] Crypto weekend 30-min cadence
- [ ] DB isolation guard
- [ ] Prop firm rules from config
- [ ] IBKR adapter ready to swap (Monday)
- [ ] --backfill-days, --dry-run, --path flags work
- [ ] No imports from v2_*.py
- [ ] daily_conviction = 0 for now

---

## 14. Out of Scope
- Factor computation (V3)
- Model scoring (V4B/V5)
- Selection (V5)
- Risk filters (V6)
- Orchestration (V7)
- Execution / SignalStack
- Daily system integration (daily_conviction — later)
- Options data / OPRA (future enhancement)
