# CODEX — Fix V6 Readiness Gate + Boot Delay + Grok Items

## Context
Vanguard predictions are now working (LONG% = 44.3%, edge_score 0.02-0.99).
But V6 rejects ALL candidates. Also the orchestrator has a ~50 second delay
BEFORE V1 even starts (this predates IBKR — was happening yesterday too).

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-v6-fix"
```

---

## PROBLEM 1: V6 Readiness Gate Rejects Everything

Rejection reasons from vanguard_tradeable_portfolio:
```
AMZN|REJECTED|readiness=live_native, threshold=0.500, prob=0.300
BKR|REJECTED|readiness=live_native, threshold=0.500, prob=0.400
ET|REJECTED|readiness=live_native, threshold=0.500, prob=0.067
```

V6 has a `_apply_readiness_gate()` function that checks `ml_prob >= threshold`.
The threshold is 0.500 (from config/vanguard_ml_thresholds.json).
The `ml_prob` values (0.067-0.433) are NOT edge_scores — they're coming from
the old classifier architecture.

We switched from classifiers to regressors. The readiness gate should use
edge_score (0.01-0.99 from cross-sectional ranking) instead of ml_prob.

File: `~/SS/Vanguard/stages/vanguard_risk_filters.py`

### Fix Option A: Disable the readiness gate entirely (fastest)
In `_apply_readiness_gate()`, make it always return APPROVED:

```python
def _apply_readiness_gate(candidate):
    # Readiness gate disabled — using regressor edge_score instead of classifier prob
    candidate["status"] = "APPROVED"
    candidate["model_readiness"] = "live_native"
    return candidate
```

### Fix Option B: Use edge_score instead of ml_prob (proper fix)
Change the gate to read edge_score from the shortlist:

```python
def _apply_readiness_gate(candidate):
    readiness = str(candidate.get("model_readiness") or "live_native")
    edge_score = float(candidate.get("edge_score") or 0.0)
    
    # New regressor architecture uses cross-sectional ranking (0.0-1.0)
    # No threshold needed — the ranking already handles quality
    if edge_score > 0:
        candidate["status"] = "APPROVED"
    else:
        candidate["status"] = "REJECTED"
        candidate["rejection_reason"] = f"edge_score={edge_score:.4f}"
    
    candidate["model_readiness"] = readiness
    return candidate
```

### Also: edge_score must flow from V5 shortlist to V6

Check if V5 writes edge_score to the shortlist table:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_shortlist_v2)" | grep edge
```

If edge_score is NOT in the table, add it:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
ALTER TABLE vanguard_shortlist_v2 ADD COLUMN edge_score REAL DEFAULT 0.0;
"
```

Then update V5 (vanguard_selection_simple.py) to write edge_score to the table.

Then update V6 to read edge_score from the shortlist:
```bash
grep -n "ml_prob\|edge_score\|shortlist\|predictions" \
  ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -20
```

### Also: Clean up duplicate account profiles
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, max_positions FROM account_profiles ORDER BY id
"
```

There are duplicates: lowercase `ttp_20k_swing` (max 5) and uppercase `TTP_20K_SWING` (max 20).
Delete the lowercase one:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
DELETE FROM account_profiles WHERE id = 'ttp_20k_swing';
DELETE FROM account_profiles WHERE id = 'ttp_40k_swing';
"
```

---

## PROBLEM 2: 50-Second Boot Delay (PRE-IBKR, Happens Since Yesterday)

The orchestrator takes ~50 seconds before V1 even starts.
Python imports take 0.45s. So it's in the orchestrator's __init__ or startup.

### Debug: Add timing to orchestrator startup

```bash
# Find the orchestrator's __init__ and startup code
grep -n "def __init__\|def run\|def start\|def _init\|class.*Orchestrator" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -10
```

Add timing around each startup step:
```python
import time
t0 = time.time()
# ... each init step ...
logger.info(f"[BOOT] Step X took {time.time()-t0:.1f}s")
```

Likely culprits (check each):
1. SQLite connection to 10GB vanguard_universe.db — WAL checkpoint on first connect
2. Universe builder materialization (get_active_equity_symbols hitting Alpaca API)
3. Alpaca WebSocket connection + subscription
4. Loading model .pkl files (600KB-1.4MB each)
5. Reading config files
6. Twelve Data adapter initialization

```bash
# Quick test: is it the DB connection?
time python3 -c "
import sqlite3
con = sqlite3.connect('/Users/sjani008/SS/Vanguard/data/vanguard_universe.db', timeout=30)
print(con.execute('SELECT COUNT(*) FROM vanguard_bars_5m').fetchone())
con.close()
"

# Quick test: is it Alpaca WS init?
time python3 -c "
import asyncio; asyncio.set_event_loop(asyncio.new_event_loop())
from vanguard.data_adapters.alpaca_adapter import AlpacaWSAdapter
"

# Quick test: is it universe builder?
time python3 -c "
from vanguard.helpers.universe_builder import materialize_universe
"
```

Report what takes the most time. Then fix it:
- If DB: add `PRAGMA journal_mode=WAL; PRAGMA wal_checkpoint(TRUNCATE);` at startup
- If Alpaca WS: make connection async (don't block startup)
- If Universe builder: cache results, don't re-materialize every cycle
- If model loading: lazy-load models (first time V4B is called, not at startup)

---

## PROBLEM 3: Store edge_score in vanguard_shortlist_v2 (Grok suggestion)

V5 computes edge_score in memory but doesn't write it to the DB table.
V6 needs it for the readiness gate. The UI/MCP also needs it to display picks.

In `vanguard_selection_simple.py`, find where it writes to vanguard_shortlist_v2
and add edge_score to the INSERT:

```bash
grep -n "INSERT\|write\|shortlist_v2\|to_sql\|executemany" \
  ~/SS/Vanguard/stages/vanguard_selection_simple.py | head -10
```

Make sure edge_score is included in the written rows.

---

## Verification

```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -30

# Check V6 now approves candidates
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account_id, status
"

# Check edge_score is in shortlist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, direction, predicted_return, edge_score, rank
FROM vanguard_shortlist_v2
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist_v2)
ORDER BY direction, rank
LIMIT 20
"

# Check approved trades for TTP
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, direction, edge_score, risk_dollars, shares_or_lots
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
AND status = 'APPROVED'
ORDER BY edge_score DESC
LIMIT 20
"
```

Expected: TTP_20K_SWING approves 10-20 candidates with proper edge_scores.

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: V6 readiness gate for regressor architecture + boot timing + edge_score persistence"
```
