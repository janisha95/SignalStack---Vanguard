# CODEX — Contract-Alignment Pass (Focused, No Rewrite)
# Align current code with GPT's 3 contract docs (API, Backend Data, Frontend)
# 6 items in strict priority order. Do them in order. Stop if unsure.
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Current system views
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id, name, is_system FROM userviews WHERE is_system = 1"

# 2. Current adapter output shape (S1)
grep -n "def get_candidates" ~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py | head -5
grep -n "partial_data\|provenance\|lane_status\|readiness" ~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py

# 3. Current adapter output shape (Meridian)
grep -n "def get_candidates" ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py | head -5
grep -n "partial_data\|provenance\|lane_status\|readiness" ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py

# 4. Current adapter output shape (Vanguard)
cat ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py

# 5. Current system view seeding
grep -n "system.*view\|seed\|is_system" ~/SS/Vanguard/vanguard/api/userviews.py | head -20

# 6. Current frontend null handling
grep -n "\\$0\\.00\|---\|N/A\|null\|undefined\|??\|fallback" ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx | head -30

# 7. Current Meridian view name
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id, name FROM userviews WHERE is_system = 1 AND name LIKE '%eridian%'"
```

Report ALL output.

---

## ITEM 1: Update Vanguard stance to shortlist_ready

The contracts say Vanguard should return `rows: []` with readiness metadata.
But vanguard_shortlist now has 41 real rows and vanguard_adapter returns real
candidates. Update the adapter and unified_api to reflect `shortlist_ready` state.

### File: ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py

Add readiness metadata to the adapter response. Find where the candidates list
is returned and add a readiness field:

```python
# At the end of get_candidates(), return a dict with both candidates and readiness:
def get_candidates():
    # ... existing code that builds candidates list ...

    # Determine readiness based on what exists
    import sqlite3
    con = sqlite3.connect(VANGUARD_DB)
    has_shortlist = False
    has_portfolio = False
    try:
        sl_count = con.execute("SELECT COUNT(*) FROM vanguard_shortlist").fetchone()[0]
        has_shortlist = sl_count > 0
    except Exception:
        pass
    try:
        tp_count = con.execute("SELECT COUNT(*) FROM vanguard_tradeable_portfolio WHERE status='APPROVED'").fetchone()[0]
        has_portfolio = tp_count > 0
    except Exception:
        pass
    con.close()

    if has_portfolio:
        readiness = "risk_ready"
    elif has_shortlist:
        readiness = "shortlist_ready"
    elif candidates:  # adapter returned rows
        readiness = "shortlist_ready"
    else:
        readiness = "data_ready"  # V1-V4 data exists but no shortlist

    return {
        "candidates": candidates,
        "readiness": readiness,
        "lane_status": "staged" if readiness != "live" else "live",
    }
```

### File: ~/SS/Vanguard/vanguard/api/unified_api.py

Find where vanguard adapter is called in the candidates endpoint. Update to
handle the new dict return shape and include readiness in the response:

```python
# Where vanguard candidates are fetched, handle both old (list) and new (dict) shapes:
vanguard_result = vanguard_adapter.get_candidates()
if isinstance(vanguard_result, dict):
    vanguard_rows = vanguard_result.get("candidates", [])
    vanguard_readiness = vanguard_result.get("readiness", "placeholder")
    vanguard_lane_status = vanguard_result.get("lane_status", "staged")
else:
    vanguard_rows = vanguard_result
    vanguard_readiness = "shortlist_ready" if vanguard_rows else "placeholder"
    vanguard_lane_status = "staged"
```

And in the response, include readiness when source=vanguard:

```python
# In the response dict:
if source == "vanguard":
    response["lane_status"] = vanguard_lane_status
    response["readiness"] = vanguard_readiness
```

---

## ITEM 2: Add row metadata fields to ALL adapters

Contract requires: `partial_data`, `provenance`, `lane_status`, `readiness`
on every candidate row. Add these to all three adapters.

### S1 adapter: ~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py

Find where each candidate dict is built. Add:

```python
candidate = {
    # ... existing fields ...
    "partial_data": convergence_missing or price_is_fallback,  # True when enrichment missing
    "provenance": {
        "base": "scorer_predictions",
        "convergence": "primary" if convergence_found else "missing",
        "price_source": price_source,  # "scorer_predictions" or "daily_bars" or "fallback"
    },
    "lane_status": "live",
    "readiness": "live",
}
```

Track `convergence_missing` — set True when convergence JSON lookup fails for this row.
Track `price_is_fallback` — set True when price came from daily_bars instead of scorer_predictions.

### Meridian adapter: ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py

Find where each candidate dict is built. Add:

```python
candidate = {
    # ... existing fields ...
    "partial_data": predictions_missing or price_is_fallback,
    "provenance": {
        "base": "shortlist_daily",
        "predictions_daily": "joined" if has_predictions else "missing",
        "price_source": price_source,  # "shortlist_daily" or "daily_bars"
    },
    "lane_status": "live",
    "readiness": "live",
}
```

Track `predictions_missing` — True when LEFT JOIN to predictions_daily returns NULL for lgbm fields.
Track `price_is_fallback` — True when price came from daily_bars.

### Vanguard adapter: ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py

Already returns rows. Add to each candidate dict:

```python
candidate = {
    # ... existing fields ...
    "partial_data": False,  # Vanguard shortlist is self-contained
    "provenance": {
        "base": "vanguard_shortlist",
        "cycle": row["cycle_ts_utc"],
    },
    "lane_status": "staged",
    "readiness": "shortlist_ready",
}
```

---

## ITEM 3: Add sys_vanguard system view

### File: ~/SS/Vanguard/vanguard/api/userviews.py

Find the system view seeding code. There should be 3 views (Combined, Meridian All, S1 All).
Add a 4th:

```python
# Add alongside existing system views:
{
    "id": "sys_vanguard",
    "name": "Vanguard",
    "is_system": True,
    "source": "vanguard",
    "filters": None,
    "sorts": None,
    "columns": None,
}
```

Seed it the same way the other 3 are seeded. Use INSERT OR IGNORE so it
doesn't duplicate on restart.

### Verify:

```bash
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json; d=json.load(sys.stdin)
for v in d.get('views', d):
    if isinstance(v, dict) and v.get('is_system'):
        print(f'  {v[\"name\"]:25s} source={v.get(\"source\",\"?\")}')
"
# Should show 4 system views: Combined, Meridian All, S1 All, Vanguard
```

---

## ITEM 4: Fix frontend truth-loss / null defaulting

Contract says: "Do not coerce missing business fields to fake defaults."

### File: ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx

Find where null values are rendered with fake defaults and fix:

**Price:** Find `$0.00` or `price ?? 0` or `price || 0` patterns.
Replace with dash display:

```tsx
// WRONG: {formatPrice(row.price || 0)}
// RIGHT: {row.price != null ? formatPrice(row.price) : '—'}
```

**Tier:** Find `tier || '---'` or `tier ?? '---'` patterns.
Replace with empty/dash:

```tsx
// WRONG: {row.tier || '---'}
// RIGHT: {row.tier || '—'}
// (This is OK as long as '—' is understood as "no tier", not a fake tier name)
```

**Lane-native scores:** Find any place where null S1 scores show as 0 for
Meridian rows or vice versa. Ensure null renders as '—' not '0' or '0.000'.

```tsx
// WRONG: {row.scorer_prob?.toFixed(3) || '0.000'}
// RIGHT: {row.scorer_prob != null ? row.scorer_prob.toFixed(3) : '—'}
```

**partial_data indicator:** If `row.partial_data === true`, show a small
warning indicator. This can be a simple chip or icon:

```tsx
{row.partial_data && <span className="text-amber-400 text-xs ml-1" title="Incomplete enrichment">⚠</span>}
```

---

## ITEM 5: Enforce lane-aware Combined sorting/filtering

Contract says lane-native fields (scorer_prob, final_score, tcn_score, etc.)
should not be sortable across all lanes in Combined mode.

### File: ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx

Find the column sort handlers. When source === "combined", disable sort on
lane-native columns:

```tsx
// In the sort handler or column header click:
const LANE_NATIVE_COLUMNS = [
  'scorer_prob', 'p_tp', 'nn_p_tp', 'convergence_score', 'scorer_rank',  // S1
  'final_score', 'tcn_score', 'factor_rank', 'predicted_return', 'residual_alpha',  // Meridian
  'strategy_score', 'ml_prob', 'edge_score', 'consensus_count',  // Vanguard
];

const isLaneNative = LANE_NATIVE_COLUMNS.includes(columnKey);
const isCombined = currentSource === 'combined';

if (isLaneNative && isCombined) {
  // Option A: Disable the sort entirely
  return; // no-op

  // Option B: Show a tooltip explaining why
  // "Sort by {column} requires a single-source view"
}
```

Also in the column header, visually indicate non-sortable columns in Combined:

```tsx
// In the <th> element for lane-native columns when combined:
<th
  className={isLaneNative && isCombined ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}
  onClick={isLaneNative && isCombined ? undefined : () => handleSort(col)}
  title={isLaneNative && isCombined ? `Sort by ${col} requires single-source view` : `Sort by ${col}`}
>
```

---

## ITEM 6: Rename Meridian view to match live truth

Contract says: "Meridian Latest Shortlist is the honest live Meridian view
until Meridian itself produces a true Top 100 artifact."

Check what the current Meridian system view is named:

```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id, name FROM userviews WHERE is_system = 1 AND name LIKE '%eridian%'"
```

If it's named "Meridian All" or "Meridian Top 100", rename to
"Meridian Latest Shortlist" to match the contract:

### File: ~/SS/Vanguard/vanguard/api/userviews.py

Find the Meridian system view seed and update the name:

```python
# FROM:
{"name": "Meridian All", ...}
# TO:
{"name": "Meridian Latest Shortlist", ...}
```

Also update the DB directly for existing installs:

```python
# In the seed/migrate function:
con.execute("""
    UPDATE userviews SET name = 'Meridian Latest Shortlist'
    WHERE is_system = 1 AND name IN ('Meridian All', 'Meridian Top 100')
""")
```

---

## VERIFY ALL

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# 1. Vanguard readiness
curl -s "http://localhost:8090/api/v1/candidates?source=vanguard" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'Vanguard readiness: {d.get(\"readiness\", \"MISSING\")}')
print(f'Vanguard lane_status: {d.get(\"lane_status\", \"MISSING\")}')
print(f'Vanguard rows: {d.get(\"total\", len(d.get(\"rows\", [])))}')
"

# 2. Row metadata on S1
curl -s "http://localhost:8090/api/v1/candidates?source=s1" | python3 -c "
import sys, json; d=json.load(sys.stdin)
row = d.get('rows', [{}])[0] if d.get('rows') else {}
print(f'partial_data: {row.get(\"partial_data\", \"MISSING\")}')
print(f'provenance: {row.get(\"provenance\", \"MISSING\")}')
print(f'lane_status: {row.get(\"lane_status\", \"MISSING\")}')
"

# 3. Row metadata on Meridian
curl -s "http://localhost:8090/api/v1/candidates?source=meridian" | python3 -c "
import sys, json; d=json.load(sys.stdin)
row = d.get('rows', [{}])[0] if d.get('rows') else {}
print(f'partial_data: {row.get(\"partial_data\", \"MISSING\")}')
print(f'provenance: {row.get(\"provenance\", \"MISSING\")}')
print(f'lane_status: {row.get(\"lane_status\", \"MISSING\")}')
"

# 4. System views (should be 4)
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json; d=json.load(sys.stdin)
system = [v for v in d.get('views', d) if isinstance(v, dict) and v.get('is_system')]
for v in system:
    print(f'  {v[\"name\"]}')
print(f'Total system views: {len(system)}')
"

# 5. Frontend build
cd ~/SS/Meridian/ui/signalstack-app && npm run build

# 6. Compile check
python3 -m py_compile ~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/api/userviews.py
```
