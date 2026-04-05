# STAGE V2: Vanguard Live Health Monitor — vanguard_prefilter.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 28, 2026
**Depends on:** Stage V1 (vanguard_cache.py must be running and populating bars)

---

## What Stage V2 Does

Lightweight live tradability gate for the FTMO/forex/futures universe (187
pre-curated instruments). Runs every 5 minutes during active sessions.
Answers: "Which of these instruments are healthy to trade RIGHT NOW?"

V2 does NOT handle equities. The 12K → 1,500-2,000 equity prefilter lives
inside V1. V2 only monitors pre-curated instruments that don't need heavy
filtering.

---

## Why V2 is Thin

The FTMO universe is already curated — no junk to remove. V2 catches
runtime issues only: stale data, session closures, spread blowouts,
volume collapse, and warm-up periods.

Expected: 150-180 survivors out of 187 during full overlap hours.

---

## 1. Input Contract

| Input | Source | Notes |
|---|---|---|
| vanguard_bars_1m | V1 DB | Freshness check |
| vanguard_bars_5m | V1 DB | Volume/spread checks |
| vanguard_universe.json | Config | 187 FTMO instruments + sessions |
| vanguard_cache_meta | V1 DB | Service state |
| vanguard_prop_firm_rules.json | Config | Per-firm thresholds |

### Config Thresholds

```json
{
    "health_monitor": {
        "max_stale_minutes": 10,
        "max_spread_multiple": 3.0,
        "min_bars_since_session_open": 3,
        "volume_thresholds": {
            "forex": 50,
            "index": 30,
            "metal": 20,
            "agriculture": 5,
            "energy": 10,
            "equity_cfd": 10,
            "crypto": 10
        }
    }
}
```

### CLI

| Flag | Default | Notes |
|---|---|---|
| --dry-run | False | Print only |
| --asset-class | all | Filter to one |
| --verbose | False | Per-instrument detail |

---

## 2. Five Health Checks

| # | Check | Condition | Status if fail |
|---|---|---|---|
| 1 | Session active | Asset class market open? | SESSION_CLOSED |
| 2 | Data fresh | Latest 1m bar within 10 min? | STALE_DATA |
| 3 | Minimum volume | 5m tick_volume >= per-class threshold? | LOW_VOLUME |
| 4 | Spread OK | Current spread <= 3x average? | WIDE_SPREAD |
| 5 | Warm-up | >= 3 5m bars since session open? | WARMING_UP |

Checks run in order. First failure excludes the instrument for this cycle.

---

## 3. Output

### Table: vanguard_health_results

```sql
CREATE TABLE IF NOT EXISTS vanguard_health_results (
    cycle_ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL,
    latest_bar_ts_utc TEXT,
    tick_volume_5m INTEGER,
    spread_current INTEGER,
    spread_avg INTEGER,
    bars_in_session INTEGER,
    session_open_ts_utc TEXT,
    PRIMARY KEY (cycle_ts_utc, symbol)
);
CREATE INDEX IF NOT EXISTS idx_vg_health_status
    ON vanguard_health_results(cycle_ts_utc, status);
```

### In-Memory: Survivor list passed to V3

Combined with V1's equity survivors by the orchestrator:

```python
equity_survivors = get_equity_survivors_from_v1(db)     # ~1,500-2,000
ftmo_survivors = run_health_monitor(db, ftmo_universe)   # ~150-180
all_survivors = equity_survivors + ftmo_survivors         # ~1,650-2,180
pass_to_factor_engine(all_survivors)                      # → V3
```

### Expected Survivors by Time

| Time (ET) | Active Sessions | Survivors |
|---|---|---|
| Sunday 5 PM | Forex, metals, energy | ~50-60 |
| Monday 2 AM | + EU indices/equities | ~100-120 |
| Monday 9:30 AM | + US equity CFDs | ~170-180 |
| Monday 4 PM | US equity CFDs close | ~130-140 |
| Saturday | Crypto only | ~20-25 |

---

## 4. Performance

Must complete in **< 5 seconds** for 187 instruments.
Pure DB reads + simple comparisons. No heavy computation.

---

## 5. Failure Handling

### Hard Fail
- Vanguard DB unavailable → abort
- V1 service_state = STOPPED → abort
- Zero survivors when sessions active → warn loudly

### Soft Warning
- Individual instrument goes OK → STALE → log transition
- Spread removes > 20% of active instruments → warn (market event)
- Count < 50% of expected → warn

---

## 6. Logging

```text
[V2] Health: 172 OK / 187 total | {OK: 172, SESSION_CLOSED: 0, STALE: 2, LOW_VOLUME: 8, WIDE_SPREAD: 1, WARMING_UP: 4}
[V2] Excluded: HEATOIL.c (STALE, 12 min), SUGAR.c (LOW_VOLUME, tv=3)
[V2] Completed in 1.2s
```

---

## 7. File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_prefilter.py       # V2 entry point
├── vanguard/
│   └── helpers/                    # Shared with V1 (session_manager, db, clock)
└── tests/
    └── test_vanguard_prefilter.py
```

No new helpers. Reuses V1's shared modules.

---

## 8. Acceptance Criteria

- [ ] stages/vanguard_prefilter.py exists and runs
- [ ] Reads from vanguard_universe.db only
- [ ] 5 health checks: session, freshness, volume, spread, warm-up
- [ ] Per-asset-class volume thresholds from config
- [ ] Writes vanguard_health_results to DB
- [ ] Returns survivor list for V3
- [ ] < 5 seconds for 187 instruments
- [ ] --dry-run, --verbose, --asset-class flags
- [ ] No imports from v2_*.py (Meridian)
- [ ] Reuses V1 shared helpers

---

## 9. Out of Scope

- Equity prefilter (inside V1)
- Factor computation (V3)
- Model scoring (V4B/V5)
- Selection (V5)
- Risk filters (V6)
- Orchestration (V7)
- Price floor / junk filter (FTMO is pre-curated)
- Earnings day block (equity-only, in V1)
