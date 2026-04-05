# CLAUDE CODE TASK: Timezone Audit — UTC Storage vs ET Display Compliance

## Design Rule (from specs — DO NOT CHANGE THIS)

The canonical rule across all 3 codebases is:
- **STORE** all timestamps in DB as **UTC ISO-8601**
- **DISPLAY** all operator-facing times in **America/New_York (ET)**
- **SESSION LOGIC** uses **America/New_York (ET)**

UTC storage is correct and intentional. DO NOT change DB storage to ET.
The investigation is: **where is UTC being shown to the operator without ET conversion?**

## What to audit

For each codebase, categorize every timestamp usage into:
1. **DB storage** (should be UTC) — verify it IS UTC
2. **Operator display** (logs, Telegram, API responses, print statements) — should be ET
3. **Session/market logic** (market open/close, EOD flatten, no-new-trades) — should use ET
4. **Internal computation** (bar comparisons, freshness checks) — UTC is fine

## Codebase 1: Advance (S1)

```bash
cd ~/SS/Advance

# Find all timezone-related code
grep -rn "utc\|UTC\|timezone\|tz\|ET\|America/New_York\|eastern\|datetime.now\|datetime.utcnow\|strftime\|isoformat" \
  --include="*.py" \
  agent_server.py s1_orchestrator_v2.py s1_morning_report_v2.py s1_evening_report_v2.py \
  fast_universe_cache.py 2>/dev/null | head -80

# Check what timezone helpers exist
grep -rn "def.*_et\|def.*eastern\|def.*utc\|def.*timezone\|def.*now_" \
  --include="*.py" . 2>/dev/null | head -20

# Check Telegram messages — are times shown in ET?
grep -n "strftime\|format.*time\|ET\|UTC" s1_morning_report_v2.py s1_evening_report_v2.py 2>/dev/null | head -20

# Check orchestrator logging
grep -n "datetime\|time\|strftime" s1_orchestrator_v2.py 2>/dev/null | head -20
```

## Codebase 2: Meridian

```bash
cd ~/SS/Meridian

# All timezone usage
grep -rn "utc\|UTC\|timezone\|tz\|ET\|America/New_York\|eastern\|datetime.now\|datetime.utcnow" \
  --include="*.py" stages/ config/ 2>/dev/null | head -80

# Timezone helpers
grep -rn "def.*_et\|def.*eastern\|def.*utc\|now_et\|today_et" \
  --include="*.py" stages/ 2>/dev/null | head -20

# Orchestrator time display
grep -n "strftime\|format.*time\|print.*time\|log.*time" stages/v2_orchestrator.py 2>/dev/null | head -20

# API responses — do they return UTC or ET?
grep -n "strftime\|isoformat\|timestamp\|datetime" stages/v2_api_server.py 2>/dev/null | head -30

# Forward tracker — what timezone are pick snapshots in?
grep -n "datetime\|utc\|timezone" stages/v2_forward_tracker.py 2>/dev/null | head -20
```

## Codebase 3: Vanguard

```bash
cd ~/SS/Vanguard

# All timezone usage
grep -rn "utc\|UTC\|timezone\|tz\|ET\|America/New_York\|eastern\|datetime.now\|datetime.utcnow" \
  --include="*.py" stages/ vanguard/ 2>/dev/null | head -100

# Clock/time helpers
cat vanguard/helpers/clock.py 2>/dev/null || echo "No clock.py"
cat vanguard/helpers/market_sessions.py 2>/dev/null || echo "No market_sessions.py"

# What does the orchestrator log look like? (we've seen this — logs show ET but DB stores UTC)
grep -n "strftime\|format.*time\|log.*time\|cycle_ts\|timestamp" stages/vanguard_orchestrator.py 2>/dev/null | head -20

# EOD flatten time check — must use ET not UTC
grep -n "flatten\|eod\|15:50\|15:30\|16:00\|no_new" stages/vanguard_orchestrator.py vanguard/helpers/eod_flatten.py 2>/dev/null | head -20

# Unified API — what timezone do API responses use?
grep -n "strftime\|isoformat\|datetime\|utc\|timezone" vanguard/api/unified_api.py 2>/dev/null | head -30

# Session logic — must compare in ET
grep -n "session\|market_open\|market_close\|is_open\|session_start\|session_end" \
  vanguard/helpers/clock.py vanguard/helpers/market_sessions.py stages/vanguard_prefilter.py 2>/dev/null | head -30

# Twelve Data adapter — bar timestamps
grep -n "timestamp\|datetime\|utc\|tz" vanguard/data_adapters/twelvedata_adapter.py 2>/dev/null | head -20
```

## Report Format

For each codebase, produce a table:

```
## [Codebase Name] — Timezone Audit

### CORRECT (UTC storage)
| File:Line | Usage | Verdict |
|---|---|---|
| stages/v2_orchestrator.py:45 | DB write: cycle_ts = datetime.utcnow().isoformat() | ✅ Correct |

### CORRECT (ET display)
| File:Line | Usage | Verdict |
|---|---|---|
| s1_morning_report.py:120 | Telegram: formatted in ET via now_et() | ✅ Correct |

### BUG — UTC shown to operator (should be ET)
| File:Line | Usage | Fix Needed |
|---|---|---|
| stages/v2_orchestrator.py:88 | print(f"Pipeline done at {datetime.utcnow()}") | Convert to ET for log |

### BUG — ET stored in DB (should be UTC)
| File:Line | Usage | Fix Needed |
|---|---|---|
| (hopefully none) | | |

### BUG — Session logic using UTC (should be ET)
| File:Line | Usage | Fix Needed |
|---|---|---|
| vanguard_prefilter.py:150 | if now_utc.hour >= 16: CLOSED | Should use ET |

### UNCLEAR — needs manual review
| File:Line | Usage | Notes |
|---|---|---|
```

### Summary counts:
- Total timestamp usages found: X
- Correct UTC storage: X
- Correct ET display: X
- BUG — UTC shown to operator: X
- BUG — ET in DB: X
- BUG — Session logic in UTC: X
- Unclear: X

## Rules
- DO NOT change any code in this task — AUDIT ONLY
- DO NOT change DB storage from UTC to anything else
- The fix for "UTC shown to operator" is to add ET conversion at the display layer, not to change storage
- Session logic (market hours, EOD flatten, no-new-trades-after) MUST use ET
- Bar slot timestamps (09:35, 09:40, etc.) are ET concepts — comparisons must use ET
- Freshness checks comparing "last bar time" to "now" should use UTC for both sides (that's fine)
- Forex session boundaries (Sun 5PM ET → Fri 5PM ET) must use ET
- Crypto sessions can use UTC (24/7 doesn't have timezone issues)
