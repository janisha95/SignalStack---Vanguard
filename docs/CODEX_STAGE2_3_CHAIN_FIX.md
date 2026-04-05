# CODEX — Stage 2-3 Chain Fix
# Make V2→V3 a real production chain, not two disconnected modules
# Based on GPT Stage 2-3 Gap Analysis — 9 blockers identified
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Current V2 prefilter
head -50 ~/SS/Vanguard/stages/vanguard_prefilter.py
grep -n "def run\|def main\|def prefilter\|session\|market_hours\|equity\|forex" \
  ~/SS/Vanguard/stages/vanguard_prefilter.py | head -20

# 2. Current V3 factor engine
head -50 ~/SS/Vanguard/stages/vanguard_factor_engine.py
grep -n "def run\|def main\|survivors\|vanguard_health\|vanguard_features" \
  ~/SS/Vanguard/stages/vanguard_factor_engine.py | head -20

# 3. How V7 calls V2 and V3
grep -n "v2\|v3\|prefilter\|factor\|health\|features\|survivors" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30

# 4. Current V2 output
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT status, asset_class, COUNT(*) FROM vanguard_health
GROUP BY status, asset_class
"

# 5. Current V3 output
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT COUNT(*), COUNT(DISTINCT symbol) FROM vanguard_features
"

# 6. What universe_members has (Stage 1 canonical bus)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, COUNT(*), SUM(is_active)
FROM vanguard_universe_members GROUP BY data_source, asset_class
"

# 7. V3 factor modules
ls ~/SS/Vanguard/vanguard/factors/ 2>/dev/null || \
  ls ~/SS/Vanguard/vanguard/modules/ 2>/dev/null || \
  find ~/SS/Vanguard -path "*/factors/*.py" -o -path "*/modules/m*.py" 2>/dev/null

# 8. Session/clock helpers
grep -n "session\|market_hours\|is_open\|ET\|forex\|crypto\|24" \
  ~/SS/Vanguard/vanguard/helpers/clock.py 2>/dev/null | head -15
```

Report ALL output.

---

## THE CORE PROBLEM

V2 and V3 exist as standalone modules but are not a production chain because:
1. V2 uses equity-only session logic (9:30-16:00 ET) for ALL instruments
2. V3 doesn't reliably consume fresh V2 output each cycle
3. V3 symbol coverage is tiny vs the actual active universe
4. V7 orchestrator may skip V2 or reuse stale V2 data
5. Neither stage is multi-asset session-aware

---

## FIX 1: V2 must be multi-asset session-aware

File: `~/SS/Vanguard/stages/vanguard_prefilter.py`

V2 health checks must use the correct session window per asset class.
Read session info from `vanguard_universe_members`:

```python
SESSION_DEFINITIONS = {
    "equity": {"start": "09:30", "end": "16:00", "tz": "America/New_York", "days": "Mon-Fri"},
    "forex":  {"start": "17:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri", "note": "24/5 with daily break"},
    "crypto": {"start": None, "end": None, "tz": "UTC", "days": "Mon-Sun", "note": "24/7"},
    "index":  {"start": "09:30", "end": "16:00", "tz": "America/New_York", "days": "Mon-Fri"},
    "metal":  {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "energy": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "commodity": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
    "agriculture": {"start": "19:00", "end": "13:20", "tz": "America/New_York", "days": "Mon-Fri"},
    "futures": {"start": "18:00", "end": "17:00", "tz": "America/New_York", "days": "Sun-Fri"},
}

def is_session_active(asset_class: str) -> bool:
    """Check if the current time is within this asset class's trading session."""
    session = SESSION_DEFINITIONS.get(asset_class, SESSION_DEFINITIONS["equity"])
    if session["start"] is None:  # 24/7 (crypto)
        return True
    # ... timezone-aware check using session start/end/tz/days
```

V2 health checks per symbol must:
1. Look up asset_class from `vanguard_universe_members`
2. Check if session is active for that asset class
3. Apply session-appropriate staleness threshold (equity: no bar > 10 min, forex: no bar > 5 min during session, crypto: no bar > 15 min)
4. Mark status: ACTIVE, STALE, HALTED, CLOSED, LOW_VOLUME

---

## FIX 2: V2 must read from universe_members (canonical bus)

Instead of V2 having its own symbol list, it reads from `vanguard_universe_members`:

```python
def get_v2_universe():
    """Get all active symbols from the canonical universe bus."""
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT symbol, asset_class, data_source, session_start, session_end, session_tz
        FROM vanguard_universe_members
        WHERE is_active = 1
    """).fetchall()
    con.close()
    return rows
```

This ensures V2 operates on the same universe that V1 is ingesting.

---

## FIX 3: V7 must call V2 EVERY cycle, not skip it

File: `~/SS/Vanguard/stages/vanguard_orchestrator.py`

Find the cycle function. Ensure V2 runs BEFORE V3 on EVERY cycle:

```python
def run_cycle(self):
    # V1: data ingest (Twelve Data poll + Alpaca WS already running)
    self.run_v1()

    # V2: health check — MUST run every cycle
    survivors = self.run_v2()
    if not survivors:
        self.log("V2: 0 survivors, skipping V3-V6")
        return

    # V3: factor computation on FRESH V2 survivors
    features = self.run_v3(survivors)
    # ... V4B, V5, V6
```

V2 output (survivor list) is passed DIRECTLY to V3. No stale reuse.

---

## FIX 4: V3 must consume ONLY fresh V2 survivors

File: `~/SS/Vanguard/stages/vanguard_factor_engine.py`

V3 must NOT:
- Use its own hardcoded symbol list
- Read all symbols from the DB
- Reuse previous cycle's features

V3 MUST:
- Accept a survivor list parameter from V2
- Compute features ONLY for those survivors
- Write with the current cycle timestamp

```python
def run_factor_engine(survivors: list[dict], cycle_ts: str) -> pd.DataFrame:
    """
    Compute 35 features for V2 survivors only.
    
    Args:
        survivors: list of {symbol, asset_class} from V2
        cycle_ts: current cycle timestamp
    
    Returns:
        DataFrame with features for all survivors
    """
    if not survivors:
        return pd.DataFrame()
    
    # Group by asset class for benchmark selection
    by_class = {}
    for s in survivors:
        by_class.setdefault(s["asset_class"], []).append(s["symbol"])
    
    all_features = []
    for asset_class, symbols in by_class.items():
        # Get appropriate benchmark for this asset class
        benchmark = get_benchmark(asset_class)  # SPY for equity, DXY for forex, etc.
        
        # Compute features
        features = compute_features_for_class(symbols, asset_class, benchmark)
        all_features.append(features)
    
    result = pd.concat(all_features, ignore_index=True) if all_features else pd.DataFrame()
    result["cycle_ts_utc"] = cycle_ts
    
    # Write to DB
    write_features(result)
    
    return result
```

---

## FIX 5: V3 multi-asset factor handling

Some V3 features are asset-class-specific. Handle this explicitly:

```python
# Features that only apply to certain asset classes:
EQUITY_ONLY_FEATURES = [
    "rs_vs_benchmark_intraday",   # RS vs SPY
    "daily_rs_vs_benchmark",       # daily RS vs SPY
    "session_opening_range_position",  # equity 9:30 OR
    "gap_pct",                     # equity overnight gaps
]

FOREX_SKIP_FEATURES = [
    "gap_pct",                     # no gaps in 24/5
    "session_opening_range_position",  # no clear OR for forex
    "bars_since_session_open",     # ambiguous for 24/5
]

def compute_features_for_class(symbols, asset_class, benchmark):
    """Compute features with asset-class-aware handling."""
    # Compute all features
    features = compute_all_35(symbols, benchmark)
    
    # Set N/A features to NaN for this asset class
    if asset_class == "forex":
        for col in FOREX_SKIP_FEATURES:
            if col in features.columns:
                features[col] = float('nan')
    elif asset_class == "crypto":
        for col in FOREX_SKIP_FEATURES + ["session_phase"]:
            if col in features.columns:
                features[col] = float('nan')
    
    # Set benchmark to correct one
    # equity → SPY, forex → DXY, metal → XAU/USD, crypto → BTC/USD
    
    features["asset_class"] = asset_class
    return features
```

---

## FIX 6: V2 output contract — add cycle_ts and richer metadata

Update `vanguard_health` to include cycle timestamp and asset class:

```sql
-- Add columns if not present
ALTER TABLE vanguard_health ADD COLUMN cycle_ts_utc TEXT;
ALTER TABLE vanguard_health ADD COLUMN asset_class TEXT DEFAULT 'equity';
ALTER TABLE vanguard_health ADD COLUMN data_source TEXT;
ALTER TABLE vanguard_health ADD COLUMN session_active INTEGER DEFAULT 1;
ALTER TABLE vanguard_health ADD COLUMN last_bar_age_seconds INTEGER;
```

Every V2 run writes with the current `cycle_ts_utc` so downstream knows
this is fresh, not stale from hours ago.

---

## FIX 7: V3 output contract — explicit column storage, not JSON blobs

If V3 currently stores features as JSON blobs in `vanguard_features`,
migrate to explicit columns in `vanguard_factor_matrix` (as defined in V3 spec).

Check current schema:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_features)"
```

If it's JSON blobs, the factor matrix table from the V3 spec should be created
and V3 should write there instead. If explicit columns already exist, just
verify they match the spec.

---

## FIX 8: Chain integrity logging

At the end of each V7 cycle, log chain stats:

```python
def log_chain_integrity(v2_survivors, v3_features, cycle_ts):
    """Log V2→V3 chain health for operator visibility."""
    logger.info(
        f"[chain] cycle={cycle_ts} | "
        f"V2 survivors={len(v2_survivors)} | "
        f"V3 features={len(v3_features)} | "
        f"coverage={len(v3_features)/max(len(v2_survivors),1)*100:.0f}% | "
        f"by_class={dict(Counter(s['asset_class'] for s in v2_survivors))}"
    )
```

---

## VERIFY

```bash
# Compile
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_prefilter.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_factor_engine.py
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py

# V2 multi-asset health check
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, status, COUNT(*) FROM vanguard_health
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_health)
GROUP BY asset_class, status
"

# V3 coverage matches V2
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 'v2_survivors' as stage, COUNT(DISTINCT symbol) FROM vanguard_health WHERE status='ACTIVE'
UNION ALL
SELECT 'v3_features', COUNT(DISTINCT symbol) FROM vanguard_features
"

# Single cycle test
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --dry-run
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: V2-V3 chain — multi-asset sessions, fresh handoff, coverage alignment"
```
