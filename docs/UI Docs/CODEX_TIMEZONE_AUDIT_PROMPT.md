# CODEX — System-Wide Timezone Audit
# SignalStack: Meridian + S1 + Vanguard + Unified API + Frontend
# Rule: Store UTC in DB, display ET to operator. No exceptions.
# Date: Mar 30, 2026

---

## THE RULE

From VANGUARD_SUPPORTING_SPECS.md §4 TIME & TIMESTAMP:

> Store all timestamps in DB as **UTC ISO-8601**
> Render all operator-facing times in **America/New_York**
> Market session logic uses **America/New_York**

This applies to ALL systems — Meridian, S1, Vanguard, Unified API, Frontend.

---

## WHAT TO AUDIT

Search every Python file, every TypeScript/TSX file, every SQL schema for
timestamp handling. Report violations in a structured format.

### Violation types:

1. **NAIVE_NOW** — `datetime.now()` without timezone (gives local time, not UTC)
   - Fix: `datetime.now(timezone.utc)` or `datetime.utcnow()` with explicit UTC suffix
   
2. **SQLITE_NOW** — `datetime('now')` in SQLite DEFAULT (gives UTC but no timezone marker)
   - Fix: Store as `datetime('now') || 'Z'` or handle in Python before INSERT
   
3. **DISPLAY_UTC** — Showing raw UTC timestamps to operator without ET conversion
   - Fix: Convert to ET before display using `pytz` or `zoneinfo`
   
4. **NAIVE_STRFTIME** — `datetime.now().strftime(...)` without timezone
   - Fix: `datetime.now(ZoneInfo("America/New_York")).strftime(...)`
   
5. **HARDCODED_OFFSET** — Using `-04:00` or `-05:00` instead of proper ET (DST-aware)
   - Fix: Use `ZoneInfo("America/New_York")` which handles EST/EDT automatically

6. **FRONTEND_RAW** — Frontend displaying timestamp from API without ET conversion
   - Fix: Parse ISO string and convert to ET using `Intl.DateTimeFormat` or `toLocaleString`

---

## FILES TO AUDIT

### Meridian (~/SS/Meridian/)

```bash
# Find all timestamp usage in Meridian Python files
grep -rn "datetime.now()" stages/*.py --include="*.py"
grep -rn "datetime.utcnow()" stages/*.py --include="*.py"
grep -rn "datetime('now')" stages/*.py --include="*.py"
grep -rn "strftime" stages/*.py --include="*.py"
grep -rn "executed_at" stages/*.py --include="*.py"
grep -rn "created_at" stages/*.py --include="*.py"
grep -rn "updated_at" stages/*.py --include="*.py"
grep -rn "\.isoformat()" stages/*.py --include="*.py"
```

Key files:
- `stages/v2_orchestrator.py` — pipeline timestamps, Telegram messages
- `stages/v2_cache_warm.py` — cache_meta timestamps  
- `stages/v2_factor_engine.py` — factor_matrix_daily timestamps
- `stages/v2_selection.py` — shortlist_daily timestamps
- `stages/v2_api_server.py` — API response timestamps
- `stages/lgbm_scorer.py` — predictions_daily timestamps
- `stages/tcn_scorer.py` — TCN scoring timestamps

### S1 (~/SS/Advance/)

```bash
grep -rn "datetime.now()" ~/SS/Advance/*.py ~/SS/Advance/**/*.py --include="*.py"
grep -rn "strftime" ~/SS/Advance/*.py ~/SS/Advance/**/*.py --include="*.py"
grep -rn "datetime('now')" ~/SS/Advance/*.py ~/SS/Advance/**/*.py --include="*.py"
```

Key files:
- `agent_server.py` — 7100+ line backend, many timestamps
- `s1_morning_report_v2.py` — Telegram messages with timestamps
- `s1_evening_report_v2.py` — Telegram messages with timestamps

### Vanguard (~/SS/Vanguard/)

```bash
grep -rn "datetime.now()" ~/SS/Vanguard/ --include="*.py"
grep -rn "datetime.utcnow()" ~/SS/Vanguard/ --include="*.py"
grep -rn "datetime('now')" ~/SS/Vanguard/ --include="*.py"
grep -rn "strftime" ~/SS/Vanguard/ --include="*.py"
```

Key files:
- `vanguard/api/unified_api.py` — all API response timestamps
- `vanguard/api/trade_desk.py` — execution timestamps (CRITICAL — trade log)
- `vanguard/api/adapters/meridian_adapter.py` — as_of timestamps
- `vanguard/api/adapters/s1_adapter.py` — as_of timestamps
- `vanguard/execution/bridge.py` — execution log cycle_ts_utc
- `vanguard/execution/signalstack_adapter.py` — webhook timestamps
- `vanguard/execution/telegram_alerts.py` — alert timestamps (operator-facing!)

### Frontend (~/SS/Meridian/ui/signalstack-app/)

```bash
grep -rn "new Date(" ~/SS/Meridian/ui/signalstack-app/components/ --include="*.tsx" --include="*.ts"
grep -rn "toISOString\|toLocaleString\|toLocaleDateString\|toLocaleTimeString" ~/SS/Meridian/ui/signalstack-app/ --include="*.tsx" --include="*.ts"
grep -rn "as_of\|executed_at\|created_at\|updated_at" ~/SS/Meridian/ui/signalstack-app/ --include="*.tsx" --include="*.ts"
grep -rn "Intl.DateTimeFormat\|timeZone" ~/SS/Meridian/ui/signalstack-app/ --include="*.tsx" --include="*.ts"
```

Key files:
- `components/trades-client.tsx` — Trade Log timestamps (this is the one Shan noticed)
- `components/unified-candidates-client.tsx` — as_of display
- `lib/unified-api.ts` — API response parsing

### SQLite Schemas

```bash
# Check all DEFAULT timestamp clauses
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema" | grep -i "datetime\|timestamp\|created_at\|updated_at"
sqlite3 ~/SS/Meridian/data/v2_universe.db ".schema" | grep -i "datetime\|timestamp\|created_at\|updated_at"
sqlite3 ~/SS/Advance/data_cache/signalstack_results.db ".schema" | grep -i "datetime\|timestamp\|created_at\|updated_at"
```

---

## EXPECTED FINDINGS

Based on the codebase, these are the most likely violations:

### 1. execution_log.executed_at — CRITICAL
The execution log stores `executed_at` via `datetime('now')` which is UTC but
displayed raw in the Trade Log UI. Operator sees "2026-03-31T07:31:00" and
thinks it's 7:31 AM, but it's actually 3:31 AM ET.

**Fix (backend):** Store with explicit UTC marker:
```python
from datetime import datetime, timezone
executed_at = datetime.now(timezone.utc).isoformat()
# Produces: "2026-03-31T07:31:00+00:00"
```

**Fix (frontend):** Convert to ET before display:
```typescript
function formatET(utcString: string): string {
  const d = new Date(utcString);
  return d.toLocaleString('en-US', { 
    timeZone: 'America/New_York',
    month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
    hour12: true 
  });
  // "Mar 31, 3:31 AM"
}
```

### 2. Telegram alert timestamps — OPERATOR FACING
Files like `s1_morning_report_v2.py`, `s1_evening_report_v2.py`, and
`vanguard/execution/telegram_alerts.py` likely use `datetime.now().strftime()`
which gives local machine time (Toronto = ET, but fragile — depends on system timezone).

**Fix:** Always explicit:
```python
from zoneinfo import ZoneInfo
et_now = datetime.now(ZoneInfo("America/New_York"))
timestamp_str = et_now.strftime("%I:%M %p ET")
```

### 3. Candidates as_of timestamps
The Meridian adapter returns `as_of` as a hardcoded string like
`"2026-03-30T17:00:00-04:00"` which is correct (ET with offset). Verify S1
adapter does the same.

### 4. SQLite DEFAULT values
All `created_at TEXT DEFAULT (datetime('now'))` store UTC without timezone
marker. This is technically correct for storage but ambiguous — a future reader
doesn't know if it's UTC or local.

**Fix:** Either:
- Keep as-is (UTC by SQLite convention) and document it, OR
- Change to `DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))` for explicit Z suffix

### 5. cache_meta.updated_at
Meridian cache_meta stores timestamps that may be displayed in health checks.
Verify these are UTC and converted for display.

---

## OUTPUT FORMAT

Generate a report as `~/SS/Meridian/qa_timezone_audit.md` with this structure:

```markdown
# Timezone Audit Report
Date: {today}

## Summary
- Files audited: N
- Violations found: N
- Critical (trade execution): N
- Display-only: N

## Violations

### CRITICAL — Trade Execution Timestamps
| File | Line | Code | Violation Type | Fix |
|---|---|---|---|---|

### HIGH — Operator-Facing Display
| File | Line | Code | Violation Type | Fix |

### MEDIUM — Storage Ambiguity
| File | Line | Code | Violation Type | Fix |

### LOW — Internal Only
| File | Line | Code | Violation Type | Fix |

## Files Clean (no violations)
- file1.py
- file2.py
```

---

## DO NOT FIX — AUDIT ONLY

This is an AUDIT task. Do NOT modify any files. Generate the report only.
Shan will review the report and decide which fixes to apply.

The report goes to: `~/SS/Meridian/qa_timezone_audit.md`
