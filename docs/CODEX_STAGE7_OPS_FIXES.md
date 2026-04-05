# CODEX — Stage 7 Operational Fixes
# Based on GPT Stage 7 Go-Live Gap Analysis — 5 blockers
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Current V7 orchestrator cycle
grep -n "def run_cycle\|def run_v1\|def run_v2\|def run_v3\|def run_v5\|def run_v6" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20

# 2. Execution log tables (should be ONE canonical)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 'execution_log' as tbl, COUNT(*) FROM execution_log
UNION ALL SELECT 'vanguard_execution_log', COUNT(*) FROM vanguard_execution_log
"

# 3. Account profiles
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, is_active FROM account_profiles
"

# 4. What V7 currently calls
grep -n "import\|from.*import" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30
```

---

## FIX 1: V7 must run V1+V2 in the cycle (Blocker 1 — already addressed in Stage 1+2-3 fixes)

The Stage 1 and Stage 2-3 chain fixes already wired V1 and V2 into V7's cycle.
VERIFY this is true:

```bash
grep -n "run_v1\|run_v2\|v1_step\|v2_step\|prefilter\|poll_latest\|twelve" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py
```

If V1 and V2 are NOT in the cycle after the Stage 1/2-3 fixes, add them:

```python
def run_cycle(self):
    cycle_ts = datetime.now(timezone.utc).isoformat()
    
    # V1: data ingest
    self.run_v1()
    
    # V2: health/tradability gate
    survivors = self.run_v2()
    if not survivors:
        self.log("V2: 0 survivors")
        return
    
    # V3: factor computation on fresh V2 survivors
    features = self.run_v3(survivors)
    
    # V5: strategy routing + scoring
    shortlist = self.run_v5(features, cycle_ts)
    
    # V6: risk filters per account
    portfolios = self.run_v6(shortlist, cycle_ts)
    
    # Execute
    self.execute(portfolios, cycle_ts)
```

---

## FIX 2: Consolidate to ONE execution log (Blocker 2)

`execution_log` is the canonical table (used by trade_desk.py, unified_api.py, portfolio/live).
`vanguard_execution_log` was the old staged executor path (bridge.py).

V7 MUST write to `execution_log` only. Verify:

```bash
grep -n "execution_log\|vanguard_execution_log" ~/SS/Vanguard/stages/vanguard_orchestrator.py
grep -n "vanguard_execution_log" ~/SS/Vanguard/vanguard/execution/bridge.py
```

If V7 writes to `vanguard_execution_log`, change it to `execution_log`.
If bridge.py still writes to `vanguard_execution_log`, add a deprecation comment.

After confirming V7 only uses `execution_log`, add a clear comment:

```python
# CANONICAL: all execution writes go to execution_log (not vanguard_execution_log)
# vanguard_execution_log is deprecated — kept for historical data only
```

---

## FIX 3: Startup profile validation (Blocker 4)

At V7 startup, loudly verify account profiles exist and are active:

```python
def _validate_profiles(self):
    """Startup check: verify active account profiles exist."""
    con = sqlite3.connect(DB_PATH)
    profiles = con.execute(
        "SELECT id, name, is_active, environment FROM account_profiles WHERE is_active = 1"
    ).fetchall()
    con.close()
    
    if not profiles:
        msg = "[V7] FATAL: No active account profiles found. Cannot execute trades."
        logger.error(msg)
        self._send_telegram(f"🔴 {msg}")
        raise RuntimeError(msg)
    
    logger.info(f"[V7] {len(profiles)} active profiles:")
    for p in profiles:
        logger.info(f"  {p[0]}: {p[1]} (env={p[3]})")
    
    return profiles
```

Call this in `startup()` BEFORE entering the main loop.

---

## FIX 4: Startup data connection verification (Gap 5)

At V7 startup, verify data sources are reachable:

```python
def _verify_data_sources(self):
    """Startup check: verify data adapters can connect."""
    checks = []
    
    # Check Alpaca
    try:
        import os
        key = os.environ.get("APCA_API_KEY_ID")
        if key:
            checks.append(("Alpaca", "OK"))
        else:
            checks.append(("Alpaca", "NO API KEY"))
    except Exception as e:
        checks.append(("Alpaca", f"ERROR: {e}"))
    
    # Check Twelve Data
    try:
        td_key = os.environ.get("TWELVEDATA_API_KEY") or os.environ.get("TWELVE_DATA_API_KEY")
        if td_key:
            checks.append(("TwelveData", "OK"))
        else:
            checks.append(("TwelveData", "NO API KEY"))
    except Exception as e:
        checks.append(("TwelveData", f"ERROR: {e}"))
    
    # Check DB
    try:
        con = sqlite3.connect(DB_PATH)
        bar_count = con.execute("SELECT COUNT(*) FROM vanguard_bars_1m").fetchone()[0]
        con.close()
        checks.append(("DB", f"OK ({bar_count:,} 1m bars)"))
    except Exception as e:
        checks.append(("DB", f"ERROR: {e}"))
    
    for name, status in checks:
        logger.info(f"[V7] Data source {name}: {status}")
    
    failures = [c for c in checks if "ERROR" in c[1] or "NO API KEY" in c[1]]
    if failures:
        logger.warning(f"[V7] {len(failures)} data source issues: {failures}")
```

---

## FIX 5: Cycle lag monitoring (Gap 5)

Track how long each cycle takes and alert if it exceeds threshold:

```python
def run_cycle(self):
    cycle_start = time.time()
    cycle_ts = datetime.now(timezone.utc).isoformat()
    
    try:
        # ... V1 through V6 + execute ...
        pass
    finally:
        elapsed = time.time() - cycle_start
        self.last_cycle_duration = elapsed
        
        max_lag = self.config.get("cycle_timeout_seconds", 120)
        if elapsed > max_lag:
            msg = f"[V7] CYCLE LAG: {elapsed:.0f}s exceeds {max_lag}s limit"
            logger.warning(msg)
            self._send_telegram(f"⚠️ {msg}")
        else:
            logger.info(f"[V7] Cycle completed in {elapsed:.1f}s")
```

---

## FIX 6: Regime-aware runtime behavior (Gap 5)

If V5 returns regime=DEAD or regime=CLOSED, V7 should skip execution:

```python
# After V5 returns:
if shortlist is not None and not shortlist.empty:
    regime = shortlist.iloc[0].get("regime", "ACTIVE")
    if regime in ("DEAD", "CLOSED"):
        logger.info(f"[V7] Regime={regime}, skipping execution this cycle")
        return
```

---

## VERIFY

```bash
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py

# Verify execution_log is canonical
grep -c "vanguard_execution_log" ~/SS/Vanguard/stages/vanguard_orchestrator.py
# Should be 0 (or only in comments)

# Verify V1+V2 are in the cycle
grep -c "run_v1\|run_v2" ~/SS/Vanguard/stages/vanguard_orchestrator.py
# Should be >= 2

# Single cycle dry run
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --dry-run

# Status check
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --status
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: V7 ops — profile validation, data source checks, cycle lag, regime-aware execution, single execution log"
```
