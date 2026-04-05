# CLAUDE CODE — Session 5: UI Overhaul + Account Profiles + Live P&L
# Date: Mar 31, 2026, Market Open Day
# Shan's 4 requirements + morning report debug

---

## READ FIRST

```bash
# Morning report debug — why didn't it fire?
launchctl list | grep signalstack
launchctl print gui/$(id -u)/com.signalstack.morning 2>&1 | head -20
cat ~/Library/LaunchAgents/com.signalstack.morning.plist
# Check last run
log show --predicate 'eventMessage contains "signalstack"' --last 6h 2>/dev/null | tail -20
# Check if the script itself works
ls -la ~/SS/Advance/s1_morning_report_v2.py
# Check pmset wake schedule
pmset -g sched

# Current system state
curl -s http://localhost:8090/api/v1/health | python3 -m json.tool
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
print(f'Total views: {len(views)}')
for v in views:
    print(f'  {v[\"name\"]:35s} system={v.get(\"is_system\")} source={v[\"source\"]}')
"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT COUNT(*) FROM execution_log"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema execution_log"
```

Report ALL diagnostic output before changes.

---

## REQUIREMENT 1: Simplify Candidate Presets to 3 Views Only

Delete ALL existing system views. Replace with exactly 3:

| View Name | Source | Direction | Filters | Sorts |
|---|---|---|---|---|
| S1 All | s1 | all | none | scorer_prob:desc |
| Meridian All | meridian | all | none | final_score:desc |
| Combined | combined | all | none | source:asc, final_score:desc |

### Backend fix

File: `~/SS/Vanguard/vanguard/api/userviews.py`

Replace the `seed_system_userviews()` function entirely:

```python
SYSTEM_VIEWS = [
    {
        "id": "sys_s1_all",
        "name": "S1 All",
        "source": "s1",
        "direction": None,  # all
        "filters": [],
        "sorts": [{"field": "scorer_prob", "dir": "desc"}],
        "grouping": None,
        "visible_columns": ["symbol", "side", "price", "tier", "p_tp", "nn_p_tp",
                           "scorer_prob", "convergence_score", "n_strategies_agree",
                           "strategy", "regime"],
        "column_order": ["symbol", "side", "price", "tier", "p_tp", "nn_p_tp",
                        "scorer_prob", "convergence_score", "n_strategies_agree",
                        "strategy", "regime"],
        "display_mode": "table",
        "is_system": 1,
    },
    {
        "id": "sys_meridian_all",
        "name": "Meridian All",
        "source": "meridian",
        "direction": None,  # all
        "filters": [],
        "sorts": [{"field": "final_score", "dir": "desc"}],
        "grouping": None,
        "visible_columns": ["symbol", "side", "price", "tier", "tcn_score",
                           "factor_rank", "final_score", "predicted_return",
                           "m_lgbm_long_prob", "m_lgbm_short_prob", "regime"],
        "column_order": ["symbol", "side", "price", "tier", "tcn_score",
                        "factor_rank", "final_score", "predicted_return",
                        "m_lgbm_long_prob", "m_lgbm_short_prob", "regime"],
        "display_mode": "table",
        "is_system": 1,
    },
    {
        "id": "sys_combined",
        "name": "Combined",
        "source": "combined",
        "direction": None,  # all
        "filters": [],
        "sorts": [{"field": "source", "dir": "asc"}],
        "grouping": None,
        "visible_columns": ["symbol", "source", "side", "price", "tier", "regime"],
        "column_order": ["symbol", "source", "side", "price", "tier", "regime"],
        "display_mode": "table",
        "is_system": 1,
    },
]
```

On startup, delete all existing system views and re-seed:

```python
def seed_system_userviews():
    con = get_db()
    # Delete ALL old system views
    con.execute("DELETE FROM userviews WHERE is_system = 1")
    # Insert new ones
    for view in SYSTEM_VIEWS:
        con.execute("""
            INSERT OR REPLACE INTO userviews
            (id, name, source, direction, filters, sorts, grouping,
             visible_columns, column_order, display_mode, is_system)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            view["id"], view["name"], view["source"], view["direction"],
            json.dumps(view["filters"]), json.dumps(view["sorts"]),
            json.dumps(view["grouping"]),
            json.dumps(view["visible_columns"]), json.dumps(view["column_order"]),
            view["display_mode"], view["is_system"],
        ))
    con.commit()
    con.close()
```

Do NOT delete custom (non-system) views — Shan's manually created views survive.

Verify:
```bash
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
system = [v for v in views if v.get('is_system')]
custom = [v for v in views if not v.get('is_system')]
print(f'System: {len(system)} (expect 3)')
for v in system:
    print(f'  {v[\"name\"]}')
print(f'Custom: {len(custom)} (preserved)')
"
```

---

## REQUIREMENT 2: Delete Old Entries from Trade Log

The operator needs to bulk-delete test/missed entries from the execution log.

### Backend: Add bulk delete endpoint

File: `~/SS/Vanguard/vanguard/api/unified_api.py`

```python
@app.delete("/api/v1/execution/bulk")
async def bulk_delete_executions(body: dict):
    """Delete multiple trades by ID list, or all before a date."""
    ids = body.get("ids", [])
    before_date = body.get("before")  # ISO date string

    con = sqlite3.connect(VANGUARD_DB)

    if ids:
        placeholders = ",".join("?" * len(ids))
        cur = con.execute(f"DELETE FROM execution_log WHERE id IN ({placeholders})", ids)
    elif before_date:
        cur = con.execute("DELETE FROM execution_log WHERE executed_at < ?", (before_date,))
    else:
        con.close()
        raise HTTPException(status_code=400, detail="Provide 'ids' array or 'before' date")

    deleted = cur.rowcount
    con.commit()
    con.close()
    return {"deleted": deleted}
```

### Frontend: Add "Clear Old Trades" button to Trade Log

File: `~/SS/Meridian/ui/signalstack-app/components/trades-client.tsx`

In the Trade Log tab header, add:

```typescript
// Button next to the Trade Log tab title
<button onClick={handleClearOldTrades}>Clear Old Trades</button>

const handleClearOldTrades = async () => {
  const today = new Date().toISOString().split('T')[0];
  if (!window.confirm(`Delete all trade log entries before today (${today})? This cannot be undone.`)) return;

  const res = await fetch(`${API}/api/v1/execution/bulk`, {
    method: 'DELETE',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ before: today }),
  });
  const data = await res.json();
  alert(`Deleted ${data.deleted} entries.`);
  refreshTradeLog();  // re-fetch the log
};
```

Also add per-row delete buttons (the DELETE /api/v1/execution/{id} endpoint already exists):

```typescript
// In each trade log row, add a ✕ button:
<button onClick={() => handleDeleteTrade(row.id)}>✕</button>

const handleDeleteTrade = async (id: number) => {
  await fetch(`${API}/api/v1/execution/${id}`, { method: 'DELETE' });
  refreshTradeLog();
};
```

---

## REQUIREMENT 3: Live P&L Section

This is the biggest feature. Build a "Portfolio" section that shows real-time
P&L for open positions using live price data.

### Backend: GET /api/v1/portfolio/live

File: `~/SS/Vanguard/vanguard/api/trade_desk.py` (or new file portfolio.py)

```python
@app.get("/api/v1/portfolio/live")
async def get_live_portfolio():
    """
    Returns open positions from execution_log with live prices.
    An 'open' position is one where outcome IS NULL (not WIN/LOSE/TIMEOUT).
    Live price comes from yfinance fast_info or Alpaca last trade.
    """
    import yfinance as yf

    con = sqlite3.connect(VANGUARD_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, symbol, direction, shares, entry_price, stop_loss, take_profit,
               tier, tags, notes, executed_at, account
        FROM execution_log
        WHERE outcome IS NULL OR outcome = 'OPEN'
        ORDER BY executed_at DESC
    """).fetchall()
    con.close()

    positions = []
    total_pnl = 0
    total_exposure = 0

    for row in rows:
        symbol = row["symbol"]
        entry = row["entry_price"] or 0
        shares = row["shares"] or 0
        direction = row["direction"]

        # Get live price
        try:
            ticker = yf.Ticker(symbol)
            live_price = ticker.fast_info.get("last_price", 0) or 0
        except Exception:
            live_price = 0

        # P&L calc
        if direction == "LONG":
            pnl_per_share = live_price - entry
        else:  # SHORT
            pnl_per_share = entry - live_price

        pnl_dollars = round(pnl_per_share * shares, 2)
        pnl_pct = round(pnl_per_share / entry * 100, 2) if entry else 0
        exposure = round(live_price * shares, 2)

        total_pnl += pnl_dollars
        total_exposure += exposure

        # Distance to stop/TP
        stop = row["stop_loss"] or 0
        tp = row["take_profit"] or 0
        stop_dist_pct = round((live_price - stop) / entry * 100, 2) if entry and stop else None
        tp_dist_pct = round((tp - live_price) / entry * 100, 2) if entry and tp else None
        if direction == "SHORT":
            stop_dist_pct = round((stop - live_price) / entry * 100, 2) if entry and stop else None
            tp_dist_pct = round((live_price - tp) / entry * 100, 2) if entry and tp else None

        positions.append({
            "id": row["id"],
            "symbol": symbol,
            "direction": direction,
            "shares": shares,
            "entry_price": entry,
            "live_price": live_price,
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "exposure": exposure,
            "stop_loss": stop,
            "take_profit": tp,
            "stop_distance_pct": stop_dist_pct,
            "tp_distance_pct": tp_dist_pct,
            "tier": row["tier"],
            "account": row["account"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "executed_at": row["executed_at"],
        })

    return {
        "positions": positions,
        "summary": {
            "total_positions": len(positions),
            "total_pnl": round(total_pnl, 2),
            "total_exposure": round(total_exposure, 2),
            "winners": len([p for p in positions if p["pnl_dollars"] > 0]),
            "losers": len([p for p in positions if p["pnl_dollars"] < 0]),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
```

### Frontend: Portfolio tab on Trades page

Add a third tab: TRADE DESK | TRADE LOG | **PORTFOLIO**

The Portfolio tab auto-refreshes every 30 seconds:

```typescript
// Summary bar at top
<div className="portfolio-summary">
  <StatBox label="Open Positions" value={data.summary.total_positions} />
  <StatBox label="Total P&L" value={`$${data.summary.total_pnl}`}
    color={data.summary.total_pnl >= 0 ? 'green' : 'red'} />
  <StatBox label="Total Exposure" value={`$${data.summary.total_exposure}`} />
  <StatBox label="W / L" value={`${data.summary.winners} / ${data.summary.losers}`} />
</div>

// Position table
<table>
  <thead>
    <tr>
      <th>Symbol</th><th>Dir</th><th>Shares</th><th>Entry</th>
      <th>Live</th><th>P&L $</th><th>P&L %</th>
      <th>Stop Dist</th><th>TP Dist</th><th>Account</th>
    </tr>
  </thead>
  <tbody>
    {data.positions.map(p => (
      <tr key={p.id} className={p.pnl_dollars >= 0 ? 'winning' : 'losing'}>
        <td>{p.symbol}</td>
        <td className={p.direction === 'LONG' ? 'long' : 'short'}>{p.direction}</td>
        <td>{p.shares}</td>
        <td>${p.entry_price}</td>
        <td>${p.live_price}</td>
        <td className={p.pnl_dollars >= 0 ? 'green' : 'red'}>
          {p.pnl_dollars >= 0 ? '+' : ''}${p.pnl_dollars}
        </td>
        <td>{p.pnl_pct >= 0 ? '+' : ''}{p.pnl_pct}%</td>
        <td>{p.stop_distance_pct}%</td>
        <td>{p.tp_distance_pct}%</td>
        <td>{p.account}</td>
      </tr>
    ))}
  </tbody>
</table>
```

Auto-refresh:
```typescript
useEffect(() => {
  fetchPortfolio();
  const interval = setInterval(fetchPortfolio, 30000); // 30 sec
  return () => clearInterval(interval);
}, []);
```

---

## REQUIREMENT 4: Account Profiles (Prop Firm Modes)

Instead of manually typing "20k swing" in trade notes, create account profiles
in Settings that auto-tag trades.

### Backend: Account profiles table + endpoints

```sql
CREATE TABLE IF NOT EXISTS account_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,                -- "TTP 20K Swing"
    prop_firm TEXT NOT NULL,           -- "ttp" | "ftmo" | "topstep"
    account_type TEXT NOT NULL,        -- "swing" | "intraday"
    account_size INTEGER NOT NULL,     -- 20000, 50000, 100000
    daily_loss_limit REAL NOT NULL,    -- dollar amount
    max_drawdown REAL NOT NULL,        -- dollar amount
    max_positions INTEGER NOT NULL,
    must_close_eod INTEGER DEFAULT 0,  -- 1 for intraday
    is_active INTEGER DEFAULT 1,
    signalstack_webhook TEXT,          -- per-account webhook if different
    created_at TEXT DEFAULT (datetime('now'))
);
```

Seed with Shan's 6 profiles:

```python
ACCOUNT_PROFILES = [
    {"id": "ttp_20k_swing", "name": "TTP 20K Swing", "prop_firm": "ttp",
     "account_type": "swing", "account_size": 20000,
     "daily_loss_limit": 600, "max_drawdown": 1400, "max_positions": 5, "must_close_eod": 0},
    {"id": "ttp_40k_swing", "name": "TTP 40K Swing", "prop_firm": "ttp",
     "account_type": "swing", "account_size": 40000,
     "daily_loss_limit": 1200, "max_drawdown": 2800, "max_positions": 5, "must_close_eod": 0},
    {"id": "ttp_50k_intraday", "name": "TTP 50K Intraday", "prop_firm": "ttp",
     "account_type": "intraday", "account_size": 50000,
     "daily_loss_limit": 1000, "max_drawdown": 2000, "max_positions": 5, "must_close_eod": 1},
    {"id": "ttp_100k_intraday", "name": "TTP 100K Intraday", "prop_firm": "ttp",
     "account_type": "intraday", "account_size": 100000,
     "daily_loss_limit": 2000, "max_drawdown": 4000, "max_positions": 5, "must_close_eod": 1},
    {"id": "topstep_100k", "name": "TopStep 100K", "prop_firm": "topstep",
     "account_type": "intraday", "account_size": 100000,
     "daily_loss_limit": 2000, "max_drawdown": 3000, "max_positions": 10, "must_close_eod": 1},
    {"id": "ftmo_100k", "name": "FTMO 100K", "prop_firm": "ftmo",
     "account_type": "swing", "account_size": 100000,
     "daily_loss_limit": 5000, "max_drawdown": 10000, "max_positions": 10, "must_close_eod": 0},
]
```

Endpoints:
```
GET  /api/v1/accounts              — list all profiles
POST /api/v1/accounts              — create profile
PUT  /api/v1/accounts/{id}         — update profile
DELETE /api/v1/accounts/{id}       — delete profile
GET  /api/v1/accounts/{id}         — get single profile
```

### Frontend: Account selector in Trade Desk

In the Trade Desk header, add an account profile dropdown:

```typescript
// Account selector — determines risk limits and auto-tagging
<select value={activeProfile} onChange={handleProfileChange}>
  {profiles.map(p => (
    <option key={p.id} value={p.id}>
      {p.name} (${p.account_size.toLocaleString()})
    </option>
  ))}
</select>
```

When a profile is selected:
1. Risk summary bar updates with THAT profile's limits (not hardcoded 50K)
2. Compliance checks use THAT profile's daily_loss_limit and max_drawdown
3. All orders auto-tagged with the profile name
4. execution_log.account set to the profile ID
5. Risk per trade default adjusts (0.5-1% of account_size)

### Frontend: Trade Log filter by profile

In the Trade Log tab, add an account filter dropdown:

```typescript
<select value={logFilter} onChange={handleLogFilterChange}>
  <option value="all">All Accounts</option>
  {profiles.map(p => (
    <option key={p.id} value={p.id}>{p.name}</option>
  ))}
</select>
```

When filtered, `GET /api/v1/execution/log?account={profile_id}` returns only
trades for that account.

### Frontend: Settings page — Account Profile editor

On the Settings page, add an "Account Profiles" section:

```
┌─────────────────────────────────────────────────┐
│ ACCOUNT PROFILES                                 │
├─────────────────────────────────────────────────┤
│ TTP 20K Swing     $20,000   Active  [Edit] [✕]  │
│ TTP 40K Swing     $40,000   Active  [Edit] [✕]  │
│ TTP 50K Intraday  $50,000   Active  [Edit] [✕]  │
│ TTP 100K Intraday $100,000  Active  [Edit] [✕]  │
│ TopStep 100K      $100,000  Active  [Edit] [✕]  │
│ FTMO 100K         $100,000  Active  [Edit] [✕]  │
│ [+ Add Profile]                                  │
└─────────────────────────────────────────────────┘
```

---

## REQUIREMENT 5: Debug Morning Report

Check why `com.signalstack.morning` didn't fire at 6:30 AM.

```bash
# Is the plist loaded?
launchctl list | grep morning

# If not loaded:
launchctl load ~/Library/LaunchAgents/com.signalstack.morning.plist

# Check for errors
launchctl print gui/$(id -u)/com.signalstack.morning

# Check the script runs manually
cd ~/SS && python3 ~/SS/Advance/s1_morning_report_v2.py

# Check pmset (Mac wake schedule)
pmset -g sched
# If no wake schedule, the Mac was asleep at 6:30 AM

# Fix: ensure pmset wake is set
sudo pmset repeat wakeorpoweron MTWRF 06:25:00
```

Report findings.

---

## VERIFY ALL

```bash
# Restart API
pkill -f "uvicorn vanguard.api.unified_api"
cd ~/SS/Vanguard && python3 -m uvicorn vanguard.api.unified_api:app --host 127.0.0.1 --port 8090 &
sleep 2

# 1. Only 3 system views
curl -s http://localhost:8090/api/v1/userviews | python3 -c "
import sys, json
views = json.load(sys.stdin)
system = [v for v in views if v.get('is_system')]
print(f'System views: {len(system)} (expect 3)')
for v in system:
    print(f'  {v[\"name\"]}: source={v[\"source\"]}')
"

# 2. Bulk delete works
curl -s -X DELETE http://localhost:8090/api/v1/execution/bulk \
  -H 'Content-Type: application/json' \
  -d '{"before": "2026-01-01"}' | python3 -m json.tool

# 3. Portfolio endpoint works
curl -s http://localhost:8090/api/v1/portfolio/live | python3 -c "
import sys, json; d=json.load(sys.stdin)
print(f'Open positions: {d[\"summary\"][\"total_positions\"]}')
print(f'Total P&L: \${d[\"summary\"][\"total_pnl\"]}')
for p in d.get('positions', [])[:3]:
    print(f'  {p[\"symbol\"]} {p[\"direction\"]} entry=\${p[\"entry_price\"]} live=\${p[\"live_price\"]} pnl=\${p[\"pnl_dollars\"]}')
"

# 4. Account profiles
curl -s http://localhost:8090/api/v1/accounts | python3 -c "
import sys, json
accs = json.load(sys.stdin)
print(f'Profiles: {len(accs)} (expect 6)')
for a in accs:
    print(f'  {a[\"name\"]}: \${a[\"account_size\"]:,} {a[\"account_type\"]}')
"

# 5. Frontend build
cd ~/SS/Meridian/ui/signalstack-app && npm run build

# 6. Morning report plist
launchctl list | grep morning
```

---

## REQUIREMENT 6: Fix Automation Reliability

The morning report didn't fire because the Mac was asleep. The current
run_evening.sh uses a one-shot `sudo pmset schedule wakeorpoweron` which
only works if the evening pipeline runs successfully.

### Fix: Add recurring wake to boot_signalstack.sh

File: `~/SS/boot_signalstack.sh`

Add at the BOTTOM of the file, before the "Boot complete" log line:

```bash
# ── Recurring wake schedule (idempotent) ──────────────────────────────────────
# Ensure Mac wakes at 4:50 PM (before 5 PM evening pipeline)
# and 6:25 AM (before 6:30 AM morning report) on weekdays
sudo pmset repeat wakeorpoweron MTWRF 06:25:00 wakeorpoweron MTWRF 16:50:00 2>/dev/null || true
echo "  Wake schedule: 6:25 AM + 4:50 PM weekdays" >> "$LOG"
```

This is idempotent — `pmset repeat` replaces any existing repeat schedule.
It runs every time boot_signalstack.sh is called, which ensures the wake
schedule survives reboots.

### Also: Verify morning plist is loaded

```bash
# Check if morning plist exists and is loaded
if [ ! -f ~/Library/LaunchAgents/com.signalstack.morning.plist ]; then
    echo "❌ Morning plist not found!"
else
    launchctl list | grep morning || launchctl load ~/Library/LaunchAgents/com.signalstack.morning.plist
fi
```

### Also: Add unified API health check to health_check.sh

File: `~/SS/health_check.sh`

Add after the S1 API check:

```bash
curl -sf http://localhost:8090/api/v1/health > /dev/null 2>&1 \
    && echo "✅ Unified API: RUNNING (8090)" \
    || echo "❌ Unified API: DOWN (run: ~/SS/boot_signalstack.sh)"
```

---

## GIT

```bash
cd ~/SS/Meridian && git add -A && git commit -m "feat: 3 system views, bulk delete, live portfolio, account profiles, automation fix"
```
