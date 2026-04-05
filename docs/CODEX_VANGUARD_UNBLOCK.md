# CODEX — Unblock Vanguard Pipeline
# Fix PK, wire adapter, update docs
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# Current DB state
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 'shortlist' as tbl, COUNT(*) FROM vanguard_shortlist
UNION ALL SELECT 'tradeable', COUNT(*) FROM vanguard_tradeable_portfolio
UNION ALL SELECT 'portfolio_state', COUNT(*) FROM vanguard_portfolio_state
"

# Check PK issue — are APPROVED rows being overwritten?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, COUNT(*) FROM vanguard_tradeable_portfolio GROUP BY account_id, status
"

# Check current PK
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema vanguard_tradeable_portfolio"

# Check vanguard_adapter
cat ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py

# Check shortlist data shape
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, direction, strategy, strategy_score, ml_prob, consensus_count
FROM vanguard_shortlist LIMIT 10
"

# Check what the adapter should return (Vanguard fields in field_registry)
grep -A5 "vanguard" ~/SS/Vanguard/vanguard/api/field_registry.py | head -30
```

Report ALL output.

---

## FIX 1: vanguard_tradeable_portfolio PK — add direction

File: `~/SS/Vanguard/stages/vanguard_risk_filters.py`

### Step A: Deduplicate shortlist before V6 processing

Before V6 iterates over the shortlist, deduplicate by (symbol, direction),
keeping the row with the highest strategy_score:

```python
# At the top of the V6 processing loop, after loading shortlist:
# Deduplicate: keep best-scored row per (symbol, direction)
shortlist = shortlist.sort_values('strategy_score', ascending=False)
shortlist = shortlist.drop_duplicates(subset=['symbol', 'direction'], keep='first')
```

Find where the shortlist is loaded/iterated and add this dedup step.

### Step B: Fix the table PK

The CREATE TABLE statement needs `direction` in the PK:

```sql
-- Find and replace the CREATE TABLE statement
-- OLD: PRIMARY KEY (cycle_ts_utc, account_id, symbol)
-- NEW: PRIMARY KEY (cycle_ts_utc, account_id, symbol, direction)
```

Since the table already exists with data, migrate it:

```python
# Add this migration logic at the top of run_risk_filters or in a startup function:
import sqlite3
con = sqlite3.connect(DB_PATH)

# Check if we need to migrate
existing_pk = con.execute("PRAGMA table_info(vanguard_tradeable_portfolio)").fetchall()
has_direction_in_pk = False  # SQLite doesn't expose PK composition easily

# Safest approach: recreate the table
con.execute("DROP TABLE IF EXISTS vanguard_tradeable_portfolio_old")
con.execute("ALTER TABLE vanguard_tradeable_portfolio RENAME TO vanguard_tradeable_portfolio_old")
con.execute("""
    CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
        cycle_ts_utc TEXT NOT NULL,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        entry_price REAL,
        stop_price REAL,
        tp_price REAL,
        shares_or_lots REAL,
        risk_dollars REAL,
        risk_pct REAL,
        position_value REAL,
        intraday_atr REAL,
        edge_score REAL,
        rank INTEGER,
        status TEXT DEFAULT 'APPROVED',
        rejection_reason TEXT,
        PRIMARY KEY (cycle_ts_utc, account_id, symbol, direction)
    )
""")
# Copy old data (if any worth keeping)
con.execute("""
    INSERT OR IGNORE INTO vanguard_tradeable_portfolio
    SELECT * FROM vanguard_tradeable_portfolio_old
""")
con.execute("DROP TABLE vanguard_tradeable_portfolio_old")
con.commit()
con.close()
```

### Step C: Re-run single cycle to verify

```bash
cd ~/SS/Vanguard && python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE

# Verify APPROVED rows now persist
sqlite3 data/vanguard_universe.db "
SELECT account_id, direction, status, COUNT(*)
FROM vanguard_tradeable_portfolio
GROUP BY account_id, direction, status
ORDER BY account_id, direction
"
# Should show APPROVED > 0 per account
```

---

## FIX 2: Wire vanguard_adapter.get_candidates()

File: `~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py`

Replace the placeholder `return []` with a real implementation:

```python
import sqlite3
import json
import os

VANGUARD_DB = os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db")


def get_candidates():
    """
    Read latest Vanguard shortlist and return normalized candidate rows.
    Deduplicates by (symbol, direction) — returns best-scored row per pair.
    """
    con = sqlite3.connect(VANGUARD_DB)
    con.row_factory = sqlite3.Row

    # Get latest cycle
    try:
        latest = con.execute(
            "SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist"
        ).fetchone()[0]
    except Exception:
        con.close()
        return []

    if not latest:
        con.close()
        return []

    rows = con.execute("""
        SELECT symbol, direction, asset_class, strategy,
               strategy_rank, strategy_score, ml_prob, edge_score,
               consensus_count, strategies_matched, regime,
               cycle_ts_utc
        FROM vanguard_shortlist
        WHERE cycle_ts_utc = ?
        ORDER BY consensus_count DESC, strategy_score DESC
    """, (latest,)).fetchall()
    con.close()

    # Deduplicate by (symbol, direction) — keep highest scored
    candidates = []
    seen = set()
    for row in rows:
        key = (row["symbol"], row["direction"])
        if key in seen:
            continue
        seen.add(key)

        # Parse strategies_matched (stored as JSON string or comma-separated)
        strats = row["strategies_matched"]
        if strats:
            try:
                strats = json.loads(strats) if strats.startswith("[") else strats.split(",")
            except Exception:
                strats = [strats]
        else:
            strats = []

        candidates.append({
            "row_id": f"vanguard:{row['symbol']}:{row['direction']}:{latest}",
            "source": "vanguard",
            "symbol": row["symbol"],
            "side": row["direction"],
            "price": 0,  # No price in shortlist — frontend can fetch live
            "as_of": latest,
            "tier": f"vanguard_{row['direction'].lower()}",
            "sector": row["asset_class"] or "UNKNOWN",
            "regime": row["regime"] or "UNKNOWN",
            "native": {
                "strategy": row["strategy"],
                "strategy_rank": row["strategy_rank"],
                "strategy_score": round(row["strategy_score"], 4) if row["strategy_score"] else None,
                "ml_prob": round(row["ml_prob"], 4) if row["ml_prob"] else None,
                "edge_score": round(row["edge_score"], 4) if row["edge_score"] else None,
                "consensus_count": row["consensus_count"] or 0,
                "strategies_matched": strats,
                "asset_class": row["asset_class"],
            },
        })

    return candidates
```

### Verify:

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# Test Vanguard source
curl -s "http://localhost:8090/api/v1/candidates?source=vanguard" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'Vanguard candidates: {d.get(\"total\", len(d.get(\"rows\", [])))}')
for r in d.get('rows', [])[:5]:
    print(f'  {r[\"symbol\"]:8s} {r[\"side\"]:5s} strategy={r[\"native\"].get(\"strategy\")} score={r[\"native\"].get(\"strategy_score\")} consensus={r[\"native\"].get(\"consensus_count\")}')
"
# Should return real Vanguard candidates, NOT empty
```

---

## FIX 3: Update stale docs

File: `~/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md`

Find and replace these stale status claims:

```
# FIND:
| V5 Selection | `stages/vanguard_selection.py` | — | ❌ NOT BUILT |

# REPLACE WITH:
| V5 Selection | `stages/vanguard_selection.py` | 26 | ✅ DONE |
```

```
# FIND:
| V6 Risk | `stages/vanguard_risk_filters.py` | — | ❌ NOT BUILT |

# REPLACE WITH:
| V6 Risk | `stages/vanguard_risk_filters.py` | 35 | ✅ DONE |
```

Also update the "What's NOT Built Yet" section to remove V5 and V6 from it.
Only V7 Orchestrator remains not built.

Also add a note about the V6 Pre-Execution Gate:

```
### V6 Enhancement: Pre-Execution Gate
- 10 risk checks wired into POST /api/v1/execute
- Checks: account active, DLL headroom, DD headroom, position count,
  single-position concentration, batch exposure, total portfolio risk,
  duplicate symbols, sector cap, TTP min stop range
- Returns approved[] + rejected[] before any webhook call
```

---

## VERIFY ALL

```bash
# 1. PK fix
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema vanguard_tradeable_portfolio" | grep "PRIMARY KEY"
# Should include direction

# 2. Re-run cycle
cd ~/SS/Vanguard && python3 scripts/run_single_cycle.py --skip-cache --force-regime ACTIVE

# 3. Check approved rows
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, COUNT(*) FROM vanguard_tradeable_portfolio GROUP BY account_id, status
"

# 4. Adapter returns candidates
curl -s "http://localhost:8090/api/v1/candidates?source=vanguard" | python3 -c "
import sys, json; d=json.load(sys.stdin); print(f'Vanguard: {d.get(\"total\", 0)} candidates')
"

# 5. Docs updated
grep "NOT BUILT" ~/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md
# Should only show V7 Orchestrator
```
