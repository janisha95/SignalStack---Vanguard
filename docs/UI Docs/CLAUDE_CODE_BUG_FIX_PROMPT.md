# CLAUDE CODE — Full Bug Fix Prompt
# SignalStack Unified API + Frontend
# Date: Mar 30, 2026
# Priority: Fix all bugs before market open

---

## READ FIRST

Read these files before making any changes:

```
~/SS/Vanguard/vanguard/api/unified_api.py
~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py
~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py
~/SS/Vanguard/vanguard/api/trade_desk.py
~/SS/Vanguard/vanguard/api/userviews.py
~/SS/Vanguard/vanguard/api/field_registry.py
~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx
~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx
~/SS/Meridian/ui/signalstack-app/lib/unified-api.ts
~/SS/Meridian/ui/signalstack-app/lib/types.ts
```

Also run these diagnostics first to understand current state:

```bash
# Check what's running
curl -s http://localhost:8090/api/v1/health | python3 -m json.tool

# Check execution_log schema (need to know which version was built)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db '.schema execution_log'

# Check if extended columns exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(execution_log)"

# Check predictions_daily schema
sqlite3 ~/SS/Meridian/data/v2_universe.db "PRAGMA table_info(predictions_daily)"

# Check what system userviews exist
curl -s http://localhost:8090/api/v1/userviews | python3 -m json.tool

# Check candidate data quality
curl -s "http://localhost:8090/api/v1/candidates?source=meridian" | python3 -c "
import sys, json
data = json.load(sys.stdin)
rows = data.get('rows', [])
print(f'Total: {data.get(\"total\", 0)}')
print(f'Has source key: {\"source\" in data}')
print(f'Has as_of key: {\"as_of\" in data}')
zero_price = [r for r in rows if not r.get('price') or r['price'] == 0]
print(f'Zero/null prices: {len(zero_price)}')
tcn_half = [r for r in rows if r.get('native',{}).get('tcn_score') == 0.5]
print(f'TCN = 0.5 (fallback): {len(tcn_half)}')
tiers = {}
for r in rows:
    t = r.get('tier','unknown')
    tiers[t] = tiers.get(t, 0) + 1
print(f'Tiers: {tiers}')
"

# Same for S1
curl -s "http://localhost:8090/api/v1/candidates?source=s1" | python3 -c "
import sys, json
data = json.load(sys.stdin)
rows = data.get('rows', [])
print(f'Total: {data.get(\"total\", 0)}')
zero_price = [r for r in rows if not r.get('price') or r['price'] == 0]
print(f'Zero/null prices: {len(zero_price)}')
shorts = [r for r in rows if r.get('side') == 'SHORT']
print(f'SHORT rows: {len(shorts)}')
tiers = {}
for r in rows:
    t = r.get('tier','unknown')
    tiers[t] = tiers.get(t, 0) + 1
print(f'Tiers: {tiers}')
"

# Check which endpoints exist (404 vs 200 vs 500)
for ep in \
  "http://localhost:8090/api/v1/candidates/meridian:TRNO:LONG:2026-03-30" \
  "http://localhost:8090/api/v1/sizing/TRNO/LONG" \
  "http://localhost:8090/api/v1/execution/analytics?period=last_30_days"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$ep")
  echo "$code $ep"
done

# Check DELETE endpoint
curl -s -o /dev/null -w "%{http_code}" -X DELETE http://localhost:8090/api/v1/execution/99999

# Check PUT endpoint
curl -s -o /dev/null -w "%{http_code}" -X PUT http://localhost:8090/api/v1/execution/1 \
  -H 'Content-Type: application/json' -d '{"outcome":"WIN"}'

# Check vanguard source
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8090/api/v1/candidates?source=vanguard"

# Check picks/today tier coverage
curl -s http://localhost:8090/api/v1/picks/today | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get('tiers', []):
    print(f\"{t['tier']}: {len(t.get('picks',[]))} picks\")
print(f'Total: {data.get(\"total_picks\", 0)}')
"

# Check boot script
grep -c "8090" ~/SS/boot_signalstack.sh || echo "8090 NOT in boot script"
```

Report the output of ALL diagnostics before making any code changes.

---

## PRIORITY 1 — Backend bugs (blocking trade execution)

### Fix 1: Add DELETE /api/v1/execution/{id}

File: `~/SS/Vanguard/vanguard/api/unified_api.py` (or trade_desk.py if endpoint routing is there)

```python
@app.delete("/api/v1/execution/{execution_id}")
async def delete_execution(execution_id: int):
    con = sqlite3.connect(VANGUARD_DB)
    cur = con.execute("DELETE FROM execution_log WHERE id = ?", (execution_id,))
    con.commit()
    if cur.rowcount == 0:
        con.close()
        raise HTTPException(status_code=404, detail="Trade not found")
    con.close()
    return Response(status_code=204)
```

Verify: `curl -X DELETE http://localhost:8090/api/v1/execution/1` returns 204 or 404.

### Fix 2: Add PUT /api/v1/execution/{id}

File: same as above

```python
@app.put("/api/v1/execution/{execution_id}")
async def update_execution(execution_id: int, body: dict):
    allowed_fields = {"exit_price", "exit_date", "outcome", "notes", "tags", "pnl_dollars", "pnl_pct", "days_held"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    # tags must be stored as JSON string
    if "tags" in updates and isinstance(updates["tags"], list):
        updates["tags"] = json.dumps(updates["tags"])

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [execution_id]

    con = sqlite3.connect(VANGUARD_DB)
    cur = con.execute(f"UPDATE execution_log SET {set_clause} WHERE id = ?", values)
    con.commit()
    if cur.rowcount == 0:
        con.close()
        raise HTTPException(status_code=404, detail="Trade not found")
    con.close()
    return {"id": execution_id, "updated": list(updates.keys())}
```

### Fix 3: Fix S1 $0.00 prices — daily_bars fallback

File: `~/SS/Vanguard/vanguard/api/adapters/s1_adapter.py`

Find wherever `price` is read from `scorer_predictions`. After that line, add a fallback:

```python
# After reading price from scorer_predictions row
price = row.get("price") or 0

if not price or price == 0:
    # Fallback: get latest close from Meridian daily_bars
    try:
        meridian_con = sqlite3.connect(MERIDIAN_DB)
        fallback = meridian_con.execute(
            "SELECT close FROM daily_bars WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            (ticker,)
        ).fetchone()
        meridian_con.close()
        if fallback:
            price = fallback[0]
    except Exception:
        pass
```

Where `MERIDIAN_DB = os.path.expanduser("~/SS/Meridian/data/v2_universe.db")`

Apply the same pattern in `meridian_adapter.py` for shortlist_daily.price:

```python
price = row.get("price") or 0
if not price or price == 0:
    fallback = con.execute(
        "SELECT close FROM daily_bars WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker,)
    ).fetchone()
    if fallback:
        price = fallback[0]
```

### Fix 4: TCN score — return null instead of 0.5 when fallback

File: `~/SS/Vanguard/vanguard/api/adapters/meridian_adapter.py`

Find where `tcn_score` is read from `shortlist_daily`. The TCN scorer writes 0.5 as a fallback when factor_history is incomplete. The adapter must detect this:

```python
tcn_score = row.get("tcn_score")

# TCN 0.5 is the known fallback value when factor_history has missing columns.
# Return null so the frontend shows "—" instead of a misleading 0.500
if tcn_score is not None and tcn_score == 0.5:
    tcn_score = None
```

Then update the tier computation to handle null TCN:

```python
# Tier assignment
if tcn_score is None:
    tier = "meridian_untiered"
elif direction == "LONG" and tcn_score >= 0.70 and factor_rank >= 0.80:
    tier = "meridian_long"
elif direction == "SHORT" and tcn_score < 0.30 and factor_rank > 0.90:
    tier = "meridian_short"
else:
    tier = "meridian_untiered"
```

This also affects picks/today — tier_meridian_long and tier_meridian_short will likely be EMPTY until factor_history backfill completes. That's correct behavior — don't return picks based on a fake 0.5 score.

### Fix 5: Userview restore — grouping + format normalization

File: `~/SS/Vanguard/vanguard/api/userviews.py`

The userview DB stores filters, sorts, visible_columns, column_order as TEXT (JSON strings). The backend must return them as parsed JSON arrays/objects, not raw strings:

```python
def serialize_userview(row):
    """Convert DB row to API response with proper JSON parsing."""
    def parse_json_field(val, fallback):
        if val is None:
            return fallback
        if isinstance(val, (list, dict)):
            return val
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, (list, dict)) else fallback
        except (json.JSONDecodeError, TypeError):
            return fallback

    return {
        "id": row["id"],
        "name": row["name"],
        "source": row["source"],
        "direction": row.get("direction"),
        "filters": parse_json_field(row.get("filters"), []),
        "sorts": parse_json_field(row.get("sorts"), []),
        "grouping": parse_json_field(row.get("grouping"), None),
        "visible_columns": parse_json_field(row.get("visible_columns"), []),
        "column_order": parse_json_field(row.get("column_order"), []),
        "display_mode": row.get("display_mode", "table"),
        "is_system": bool(row.get("is_system", 0)),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
```

Apply this serialization to ALL userview endpoints (GET list, GET single, POST response, PUT response).

Also fix the S1 Short Picks system view — find the seed function and fix the source filter:

```python
# Find in seed_system_userviews() — the S1 Short Picks view
# WRONG: source="meridian" or source missing
# CORRECT:
{"name": "S1 Short Picks", "source": "s1", "direction": "SHORT", ...}
```

### Fix 6: API default source and response shape

File: `~/SS/Vanguard/vanguard/api/unified_api.py`

Find the candidates endpoint. Change default source from "combined" to "meridian":

```python
# WRONG
@app.get("/api/v1/candidates")
async def get_candidates(source: str = "combined", ...):

# CORRECT
@app.get("/api/v1/candidates")
async def get_candidates(source: str = "meridian", ...):
```

Ensure the response includes top-level `source` and `as_of`:

```python
return {
    "rows": rows,
    "total": len(rows),
    "source": source,           # ADD THIS
    "as_of": as_of_timestamp,   # ADD THIS — latest date from the adapter
    "page_size": page_size,
}
```

---

## PRIORITY 2 — Missing endpoints from contract

### Fix 7: GET /api/v1/execution/analytics

```python
@app.get("/api/v1/execution/analytics")
async def execution_analytics(period: str = "last_30_days"):
    con = sqlite3.connect(VANGUARD_DB)
    con.row_factory = sqlite3.Row

    # Date filter
    if period == "last_7_days":
        date_filter = "AND executed_at >= datetime('now', '-7 days')"
    elif period == "last_30_days":
        date_filter = "AND executed_at >= datetime('now', '-30 days')"
    else:
        date_filter = ""

    rows = con.execute(f"SELECT * FROM execution_log WHERE 1=1 {date_filter}").fetchall()
    con.close()

    def compute_stats(subset):
        wins = [r for r in subset if r["outcome"] == "WIN"]
        losses = [r for r in subset if r["outcome"] == "LOSE"]
        open_trades = [r for r in subset if r["outcome"] is None or r["outcome"] == "OPEN"]
        total_decided = len(wins) + len(losses)
        wr = len(wins) / total_decided if total_decided > 0 else None
        return {"trades": len(subset), "wins": len(wins), "losses": len(losses),
                "open": len(open_trades), "win_rate": round(wr, 3) if wr else None}

    # By tier
    by_tier = {}
    for r in rows:
        t = r["tier"]
        by_tier.setdefault(t, []).append(r)

    # By source (parse from tier name or row_id)
    by_source = {}
    for r in rows:
        src = "s1" if r["tier"].startswith("tier_") else "meridian"
        by_source.setdefault(src, []).append(r)

    # By tag (tags stored as JSON array string)
    by_tag = {}
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else []
        for tag in tags:
            by_tag.setdefault(tag, []).append(r)

    return {
        "by_tier": {k: compute_stats(v) for k, v in by_tier.items()},
        "by_source": {k: compute_stats(v) for k, v in by_source.items()},
        "by_tag": {k: compute_stats(v) for k, v in by_tag.items()},
        "period": period,
    }
```

### Fix 8: GET /api/v1/candidates/{row_id} — single candidate detail

```python
@app.get("/api/v1/candidates/{row_id}")
async def get_candidate_detail(row_id: str):
    # Parse row_id format: "meridian:TRNO:LONG:2026-03-30" or "s1:JPM:LONG:2026-03-29:scorer"
    parts = row_id.split(":")
    source = parts[0]

    if source == "meridian":
        rows = meridian_adapter.get_candidates()
    elif source == "s1":
        rows = s1_adapter.get_candidates()
    else:
        raise HTTPException(status_code=400, detail=f"Unknown source: {source}")

    match = [r for r in rows if r.get("row_id") == row_id]
    if not match:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return match[0]
```

### Fix 9: Vanguard source — graceful empty response, not 500

File: `~/SS/Vanguard/vanguard/api/adapters/vanguard_adapter.py` (or wherever vanguard adapter is)

If the adapter doesn't have data yet, return empty:

```python
def get_candidates():
    # Vanguard adapter is placeholder until V5/V6 built
    return []
```

In `unified_api.py`, make sure `source=vanguard` doesn't crash:

```python
if source == "vanguard":
    rows = vanguard_adapter.get_candidates()
    # Returns empty list — that's fine, don't crash
```

---

## PRIORITY 3 — Execution log schema alignment

Run this diagnostic first:

```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(execution_log)"
```

The contract requires these columns. If any are missing, ALTER TABLE to add them:

```sql
-- Run only the ALTER statements for columns that DON'T already exist
ALTER TABLE execution_log ADD COLUMN tags TEXT;
ALTER TABLE execution_log ADD COLUMN notes TEXT;
ALTER TABLE execution_log ADD COLUMN stop_loss REAL;
ALTER TABLE execution_log ADD COLUMN take_profit REAL;
ALTER TABLE execution_log ADD COLUMN order_type TEXT;
ALTER TABLE execution_log ADD COLUMN exit_price REAL;
ALTER TABLE execution_log ADD COLUMN exit_date TEXT;
ALTER TABLE execution_log ADD COLUMN pnl_dollars REAL;
ALTER TABLE execution_log ADD COLUMN pnl_pct REAL;
ALTER TABLE execution_log ADD COLUMN outcome TEXT;
ALTER TABLE execution_log ADD COLUMN days_held INTEGER;
```

After adding, verify POST /api/v1/execute writes to all these columns from the trade payload.

---

## PRIORITY 4 — Infrastructure

### Fix 10: Add unified API to boot script

File: `~/SS/boot_signalstack.sh`

Add this line (after the existing server starts):

```bash
# Unified API (port 8090)
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
echo "Unified API started on port 8090"
```

Note: bind to `127.0.0.1` not `0.0.0.0` — this prevents anyone on the network from hitting the execute endpoint. The React app runs on localhost anyway.

### Fix 11: Picks/today — handle TCN fallback in Meridian tiers

File: `~/SS/Vanguard/vanguard/api/trade_desk.py` (or wherever picks/today is implemented)

When TCN is null/fallback, Meridian tiers should be empty (or use a fallback tier):

```python
# In the picks/today tier builder for Meridian:
meridian_rows = meridian_adapter.get_candidates()

tier_meridian_long = []
tier_meridian_short = []

for r in meridian_rows:
    tcn = r.get("native", {}).get("tcn_score")
    fr = r.get("native", {}).get("factor_rank", 0)

    # Skip if TCN is null (fallback mode) — don't use stale thresholds
    if tcn is None:
        continue

    if r.get("side") == "LONG" and tcn >= 0.70 and fr >= 0.80:
        tier_meridian_long.append(r)
    elif r.get("side") == "SHORT" and tcn < 0.30 and fr > 0.90:
        tier_meridian_short.append(r)

# Sort by final_score descending, cap at 10/5
tier_meridian_long = sorted(tier_meridian_long, key=lambda x: x.get("native",{}).get("final_score",0), reverse=True)[:10]
tier_meridian_short = sorted(tier_meridian_short, key=lambda x: x.get("native",{}).get("final_score",0), reverse=True)[:5]
```

Always return all 7 tiers in the response, even if empty:

```python
return {
    "date": today,
    "tiers": [
        {"tier": "tier_dual", "label": "Dual Filter (S1)", "source": "s1", "picks": tier_dual},
        {"tier": "tier_nn", "label": "NN Top (S1)", "source": "s1", "picks": tier_nn},
        {"tier": "tier_rf", "label": "RF High (S1)", "source": "s1", "picks": tier_rf},
        {"tier": "tier_scorer_long", "label": "Scorer Long (S1)", "source": "s1", "picks": tier_scorer_long},
        {"tier": "tier_s1_short", "label": "S1 Shorts", "source": "s1", "picks": tier_s1_short},
        {"tier": "tier_meridian_long", "label": "Meridian Longs", "source": "meridian", "picks": tier_meridian_long},
        {"tier": "tier_meridian_short", "label": "Meridian Shorts", "source": "meridian", "picks": tier_meridian_short},
    ],
    "total_picks": sum(len(t["picks"]) for t in tiers),
}
```

---

## PRIORITY 5 — Field registry and system view audit

### Fix 12: Audit field registry for phantom fields

```bash
# Get all field keys from registry
curl -s http://localhost:8090/api/v1/field-registry | python3 -c "
import sys, json
fields = json.load(sys.stdin)
for f in fields:
    print(f'{f[\"key\"]:30s} sources={f[\"sources\"]} format={f.get(\"format\",\"?\")}')
"
```

For each field, verify it appears in actual candidate rows. If a field is in the registry but always null/missing in rows, either:
- Remove it from the registry, OR
- Fix the adapter to populate it

### Fix 13: System views — verify count and filters

```bash
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
system = [v for v in views if v.get('is_system')]
print(f'{len(system)} system views (expected: 11)')
for v in system:
    print(f'  {v[\"name\"]:30s} source={v[\"source\"]:10s} dir={v.get(\"direction\",\"all\"):6s}')
"
```

The spec calls for 11 system views. Verify each has the correct source and direction filter.

---

## VERIFICATION CHECKLIST

After all fixes, run this full verification:

```bash
echo "=== Health ==="
curl -s http://localhost:8090/api/v1/health | python3 -m json.tool

echo "=== Meridian candidates ==="
curl -s "http://localhost:8090/api/v1/candidates?source=meridian" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'total={d[\"total\"]} source={d.get(\"source\")} as_of={d.get(\"as_of\")}')
print(f'zero_price={len([r for r in d[\"rows\"] if not r.get(\"price\")])}')
print(f'tcn_0.5={len([r for r in d[\"rows\"] if r.get(\"native\",{}).get(\"tcn_score\")==0.5])}')
print(f'tcn_null={len([r for r in d[\"rows\"] if r.get(\"native\",{}).get(\"tcn_score\") is None])}')
"

echo "=== S1 candidates ==="
curl -s "http://localhost:8090/api/v1/candidates?source=s1" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'total={d[\"total\"]} source={d.get(\"source\")} as_of={d.get(\"as_of\")}')
print(f'zero_price={len([r for r in d[\"rows\"] if not r.get(\"price\")])}')
print(f'shorts={len([r for r in d[\"rows\"] if r.get(\"side\")==\"SHORT\"])}')
"

echo "=== Vanguard (should be empty, not 500) ==="
curl -s -o /dev/null -w "%{http_code}" "http://localhost:8090/api/v1/candidates?source=vanguard"

echo ""
echo "=== Picks today ==="
curl -s http://localhost:8090/api/v1/picks/today | python3 -c "
import sys, json; d=json.load(sys.stdin)
for t in d.get('tiers',[]):
    print(f'{t[\"tier\"]:25s} {len(t.get(\"picks\",[]))} picks')
print(f'Total: {d.get(\"total_picks\",0)}')
"

echo "=== DELETE endpoint ==="
curl -s -o /dev/null -w "%{http_code}" -X DELETE http://localhost:8090/api/v1/execution/99999
echo " (expect 404)"

echo "=== PUT endpoint ==="
curl -s -o /dev/null -w "%{http_code}" -X PUT http://localhost:8090/api/v1/execution/99999 \
  -H 'Content-Type: application/json' -d '{"outcome":"WIN"}'
echo " (expect 404)"

echo "=== Analytics endpoint ==="
curl -s -o /dev/null -w "%{http_code}" http://localhost:8090/api/v1/execution/analytics
echo " (expect 200)"

echo "=== System views ==="
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json; vs=json.load(sys.stdin)
sys_v = [v for v in vs if v.get('is_system')]
print(f'{len(sys_v)} system views')
for v in sys_v:
    f = v.get('filters')
    ftype = type(f).__name__
    print(f'  {v[\"name\"]:30s} filters_type={ftype} grouping={v.get(\"grouping\")}')
"

echo "=== Boot script ==="
grep "8090" ~/SS/boot_signalstack.sh && echo "8090 in boot" || echo "8090 MISSING from boot"
```

Every check should pass. If any fail, fix before marking done.

---

## GIT DISCIPLINE

Before ANY code changes:
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "pre-bugfix snapshot $(date +%Y%m%d_%H%M)"
```

After all fixes pass verification:
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: all QA bugs from operator audit — DELETE/PUT endpoints, price fallback, TCN null, userview normalization, boot script"
```
