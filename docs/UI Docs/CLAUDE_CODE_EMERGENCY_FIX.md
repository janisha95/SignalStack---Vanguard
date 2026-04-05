# CLAUDE CODE — EMERGENCY: Fix Meridian Candidates + ATR Sizing
# Shan needs to place trades and sleep. Fix everything in ONE session.
# Date: Mar 30, 2026

---

## CONTEXT

Meridian candidates page shows "0 candidates" even though the API returns 60 rows:

```bash
curl -s "http://localhost:8090/api/v1/candidates?source=meridian" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'total={d[\"total\"]}')
print(f'first row: {json.dumps(d[\"rows\"][0], indent=2)[:500] if d[\"rows\"] else \"EMPTY\"}}')
"
```

This returns total=60 but the UI shows 0. Something is filtering them out
on the frontend side or the system view has a bad filter.

---

## STEP 1: DIAGNOSE — Run these FIRST, report output

```bash
# 1a. What does the Meridian Longs system view look like?
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
for v in views:
    if 'eridian' in v.get('name','') or 'Long' in v.get('name',''):
        print(json.dumps(v, indent=2))
"

# 1b. What filters does it have?
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
for v in views:
    name = v.get('name','')
    filters = v.get('filters', [])
    sorts = v.get('sorts', [])
    direction = v.get('direction', 'all')
    source = v.get('source', '?')
    if filters or 'Meridian' in name:
        print(f'{name}: source={source} dir={direction} filters={json.dumps(filters)} sorts={json.dumps(sorts)}')
"

# 1c. What does the raw API return for direction=LONG?
curl -s "http://localhost:8090/api/v1/candidates?source=meridian&direction=LONG" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'LONG: total={d[\"total\"]}')
"

# 1d. Check if ALL direction is also empty
curl -s "http://localhost:8090/api/v1/candidates?source=meridian&direction=all" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'ALL: total={d[\"total\"]}')
for r in d['rows'][:3]:
    print(f'  {r[\"symbol\"]} side={r[\"side\"]} tier={r[\"tier\"]} price={r[\"price\"]}')
"

# 1e. Check if the frontend is even calling the right URL
# Look at the unified-api.ts file for how candidates are fetched
grep -n "candidates" ~/SS/Meridian/ui/signalstack-app/lib/unified-api.ts

# 1f. Check if direction filtering works in the backend
curl -s "http://localhost:8090/api/v1/candidates?source=meridian&direction=LONG" | python3 -c "
import sys, json; d=json.load(sys.stdin)
sides = [r['side'] for r in d['rows']]
print(f'Sides in LONG response: {set(sides)} count={len(sides)}')
"

# 1g. Check the system view filters for a tier filter that would exclude meridian_untiered
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
for v in views:
    name = v.get('name','')
    filters = v.get('filters')
    if filters and isinstance(filters, list):
        for f in filters:
            if isinstance(f, dict) and 'tier' in str(f).lower():
                print(f'VIEW \"{name}\" has tier filter: {json.dumps(f)}')
    elif filters and isinstance(filters, dict):
        for k, val in filters.items():
            if 'tier' in k.lower():
                print(f'VIEW \"{name}\" has tier filter: {k}={val}')
"
```

---

## STEP 2: LIKELY ROOT CAUSES (fix based on diagnostics)

### Cause A: System view "Meridian Longs" has a tier filter

The system view probably has a filter like `tier = "meridian_long"`. But after
the TCN null fix, ALL Meridian rows have `tier = "meridian_untiered"`. So the
filter matches 0 rows.

**Fix:** Remove the tier filter from the system view. The view's direction=LONG
already filters to longs. The tier column should just be displayed, not filtered on.

```python
# In userviews.py seed_system_userviews() — find "Meridian Longs" and remove tier filter:
# WRONG:
# {"name": "Meridian Longs", "source": "meridian", "direction": "LONG", 
#  "filters": [{"field": "tier", "op": "=", "value": "meridian_long"}]}
#
# CORRECT:
# {"name": "Meridian Longs", "source": "meridian", "direction": "LONG", "filters": []}
```

Also fix "Meridian Shorts":
```python
# WRONG: filters: [{"field": "tier", "op": "=", "value": "meridian_short"}]
# CORRECT: filters: []
```

After fixing the seed function, you also need to UPDATE the existing DB rows:

```python
# In unified_api.py startup or a migration:
con = sqlite3.connect(VANGUARD_DB)

# Fix Meridian Longs view
con.execute("""
    UPDATE userviews SET filters = '[]'
    WHERE name = 'Meridian Longs' AND is_system = 1
""")

# Fix Meridian Shorts view
con.execute("""
    UPDATE userviews SET filters = '[]'
    WHERE name = 'Meridian Shorts' AND is_system = 1
""")

# Fix S1 Short Picks view (known bug — wrong source)
con.execute("""
    UPDATE userviews SET source = 's1', direction = 'SHORT', filters = '[]'
    WHERE name LIKE '%S1%Short%' AND is_system = 1
""")

con.commit()
con.close()
```

### Cause B: Frontend filters out rows where native fields are null

The frontend might be filtering out rows where `tcn_score` is null, thinking
they're invalid. Check `unified-candidates-client.tsx` for any client-side
filtering:

```bash
grep -n "filter\|tcn_score\|null\|undefined" \
  ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx | head -30
```

If there's a client-side filter like `rows.filter(r => r.native.tcn_score)`,
remove it — null TCN is valid, it should display as "—".

### Cause C: Userview restore applies a stale filter from localStorage

The frontend persists the last working state in localStorage. If the user
previously loaded a view with a tier filter, it might still be applying that
filter even after the backend removed it.

Check for localStorage contamination:
```bash
grep -n "localStorage\|lastView\|workingState\|savedFilters" \
  ~/SS/Meridian/ui/signalstack-app/components/unified-candidates-client.tsx | head -20
```

If localStorage stores filters, the fix is to re-read from the API when a
system view is selected, not from localStorage cache.

---

## STEP 3: Fix ATR Sizing Endpoint

Add `GET /api/v1/sizing/{symbol}/{direction}` to the unified API.

File: `~/SS/Vanguard/vanguard/api/unified_api.py`

```python
@app.get("/api/v1/sizing/{symbol}/{direction}")
async def get_sizing(symbol: str, direction: str):
    """Return real ATR(14) and position sizing for a symbol."""
    import math

    meridian_db = os.path.expanduser("~/SS/Meridian/data/v2_universe.db")
    con = sqlite3.connect(meridian_db)
    rows = con.execute(
        "SELECT high, low, close FROM daily_bars WHERE ticker = ? ORDER BY date DESC LIMIT 15",
        (symbol.upper(),)
    ).fetchall()
    con.close()

    if len(rows) < 2:
        raise HTTPException(status_code=404, detail=f"Not enough bars for {symbol}")

    # Reverse to chronological (oldest first)
    rows = list(reversed(rows))

    # Compute True Range
    true_ranges = []
    for i in range(1, len(rows)):
        h, l, prev_c = rows[i][0], rows[i][1], rows[i-1][2]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    n = min(14, len(true_ranges))
    atr = sum(true_ranges[-n:]) / n if n > 0 else 0
    price = rows[-1][2]
    is_long = direction.upper() == "LONG"

    def stop_at(mult):
        dist = round(atr * mult, 4)
        sp = round(price - dist, 2) if is_long else round(price + dist, 2)
        return {"distance": dist, "price": sp}

    return {
        "symbol": symbol.upper(),
        "direction": direction.upper(),
        "price": price,
        "atr_14": round(atr, 4),
        "atr_pct": round(atr / price * 100, 2) if price else 0,
        "stops": {
            "atr_1_5": stop_at(1.5),
            "atr_2_0": stop_at(2.0),
            "atr_2_5": stop_at(2.5),
        },
    }
```

---

## STEP 4: Wire Frontend to Use Real ATR

File: `~/SS/Meridian/ui/signalstack-app/lib/unified-api.ts`

Add:
```typescript
export async function fetchSizing(symbol: string, direction: string) {
  const base = process.env.NEXT_PUBLIC_UNIFIED_API_URL || 'http://localhost:8090';
  const res = await fetch(`${base}/api/v1/sizing/${encodeURIComponent(symbol)}/${encodeURIComponent(direction)}`);
  if (!res.ok) return null;
  return res.json();
}
```

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`

Find the recalculate function. Replace the fake percentage lookup:

```typescript
// FIND this pattern (fake ATR):
// const pct = { 'atr_1.5': 0.02, 'atr_2.0': 0.027, ... }[stopMethod];
// stopLoss = entry * (1 - pct);

// REPLACE with real ATR fetch:
// When stop method is ATR-based, call the sizing API
if (order.stopMethod === 'atr_1.5' || order.stopMethod === 'atr_2.0') {
  // Fetch real ATR (cache the result for this symbol)
  const sizing = await fetchSizing(order.symbol, order.direction);
  if (sizing) {
    const stopKey = order.stopMethod === 'atr_1.5' ? 'atr_1_5' : 'atr_2_0';
    order.stopLoss = sizing.stops[stopKey].price;
  }
}
```

NOTE: The recalculate function might be synchronous. If so, you'll need to:
1. Make it async, OR
2. Fetch ATR when the order is first added and cache it on the order object, OR
3. Add an `atr` field to the TradeOrder type and fetch it once on add

The cleanest approach: when a candidate is sent to the Trade Desk (via the
"Trade" button on Candidates page), immediately fetch sizing and store
`atr_14` on the order. Then recalculate uses the cached ATR.

```typescript
// In the "add to trade desk" handler:
const sizing = await fetchSizing(symbol, direction);
const order: TradeOrder = {
  symbol,
  direction,
  // ... other fields ...
  atr: sizing?.atr_14 || 0,  // cache real ATR
};

// In recalculate (synchronous, uses cached ATR):
if (order.stopMethod === 'atr_1.5') {
  const dist = order.atr * 1.5;
  order.stopLoss = isLong
    ? Math.round((entry - dist) * 100) / 100
    : Math.round((entry + dist) * 100) / 100;
} else if (order.stopMethod === 'atr_2.0') {
  const dist = order.atr * 2.0;
  order.stopLoss = isLong
    ? Math.round((entry - dist) * 100) / 100
    : Math.round((entry + dist) * 100) / 100;
}
```

---

## STEP 5: Block Execution on TTP Compliance Failure

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`

Find the "Execute All" button. Disable it when compliance fails:

```typescript
// Compute compliance status
const maxPositions = 5;
const dailyLossLimit = 1000;  // 2% of $50K
const maxDrawdown = 2000;     // 4% of $50K

const totalRisk = orders.reduce((sum, o) => {
  const risk = Math.abs(o.entryPrice - o.stopLoss) * o.shares;
  return sum + risk;
}, 0);

const hasComplianceFail = 
  orders.length > maxPositions ||
  totalRisk > dailyLossLimit ||
  totalRisk > maxDrawdown;

// On the button:
// disabled={hasComplianceFail || orders.length === 0}
// Show red text explaining why if disabled
```

---

## STEP 6: Verify

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# 1. Meridian candidates visible
curl -s "http://localhost:8090/api/v1/candidates?source=meridian&direction=LONG" | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'LONG candidates: {d[\"total\"]}')
for r in d['rows'][:5]:
    print(f'  {r[\"symbol\"]:8s} side={r[\"side\"]:5s} tier={r[\"tier\"]:20s} price=\${r[\"price\"]}')
"

# 2. System views fixed (no tier filters on Meridian views)
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
for v in json.load(sys.stdin):
    if 'Meridian' in v.get('name',''):
        print(f'{v[\"name\"]}: filters={v[\"filters\"]} dir={v.get(\"direction\")}')
"

# 3. Sizing endpoint works
curl -s http://localhost:8090/api/v1/sizing/JPM/LONG | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'JPM: price=\${d[\"price\"]}, ATR=\${d[\"atr_14\"]}, ATR%={d[\"atr_pct\"]}%')
print(f'  1.5x stop: \${d[\"stops\"][\"atr_1_5\"][\"price\"]}')
print(f'  2.0x stop: \${d[\"stops\"][\"atr_2_0\"][\"price\"]}')
"

# 4. Frontend builds
cd ~/SS/Meridian/ui/signalstack-app && npm run build

# 5. Check Meridian view in browser
echo "Open http://localhost:3000/candidates — select Meridian source, ALL direction"
echo "Should see 60 candidates (30 LONG + 30 SHORT)"
echo "TCN Score column should show '—' for all rows"
echo "LGBM probs should show real values (0.49, 0.40, etc.)"
```

---

## GIT

```bash
cd ~/SS/Meridian && git add -A && git commit -m "fix: Meridian visibility, system view filters, ATR sizing endpoint"
```
