# CODEX — INVESTIGATE Current Vanguard V1/V2 Architecture (NO CHANGES)

## Goal
Before speccing IBKR wiring or any V1 restructuring, we need to understand
exactly what the code does TODAY after all recent patches. DO NOT FIX ANYTHING.
Just report the current state.

## Investigation 1: How does V1 work right now?

```bash
# What does the orchestrator actually do for V1?
grep -n -A5 "def.*v1\|def.*run_v1\|def.*poll\|def.*cache\|alpaca.*poll\|twelve.*poll" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -60

# How long does each V1 substep take? (Codex added timings today)
grep -n "v1.*time\|elapsed\|alpaca.*poll\|twelve.*poll\|substage" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20

# Does vanguard_cache.py still exist as a separate entry point?
ls -la ~/SS/Vanguard/stages/vanguard_cache.py 2>/dev/null
head -30 ~/SS/Vanguard/stages/vanguard_cache.py 2>/dev/null

# What does the orchestrator import from adapters?
grep -n "import.*alpaca\|import.*twelve\|from.*adapter" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -10
```

## Investigation 2: How does V2 work right now?

```bash
# What does V2 actually check?
head -80 ~/SS/Vanguard/stages/vanguard_prefilter.py

# Does V2 handle equities or just non-equity?
grep -n "equity\|asset_class\|session\|CLOSED\|ACTIVE" \
  ~/SS/Vanguard/stages/vanguard_prefilter.py | head -20

# What tables does V2 read from and write to?
grep -n "vanguard_health\|vanguard_bars\|universe_members\|prefilter" \
  ~/SS/Vanguard/stages/vanguard_prefilter.py | head -20

# Does universe_members table exist?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".tables" | tr ' ' '\n' | sort
```

## Investigation 3: How does the equity prefilter work?

```bash
# Is there a separate equity prefilter?
find ~/SS/Vanguard -name "*equity*prefilter*" -o -name "*prefilter*equity*" 2>/dev/null
grep -rn "equity_prefilter\|get_equity_survivors\|equity.*filter" \
  ~/SS/Vanguard/stages/ ~/SS/Vanguard/vanguard/ 2>/dev/null | head -20

# How does the orchestrator decide which symbols to score in V4B?
grep -n "survivors\|equity\|forex\|asset_class\|symbols.*to.*score" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

## Investigation 4: Data flow — what writes bars to the DB?

```bash
# Current bar counts by source and recency
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, 
    COUNT(*) as total_bars,
    COUNT(DISTINCT symbol) as symbols,
    MAX(bar_ts_utc) as latest_bar
FROM vanguard_bars_5m
WHERE bar_ts_utc > datetime('now', '-2 hours')
GROUP BY data_source, asset_class
ORDER BY data_source, asset_class
"

# Same for 1m
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, 
    COUNT(*) as total_bars,
    COUNT(DISTINCT symbol) as symbols,
    MAX(bar_ts_utc) as latest_bar
FROM vanguard_bars_1m
WHERE bar_ts_utc > datetime('now', '-2 hours')
GROUP BY data_source, asset_class
ORDER BY data_source, asset_class
"

# What tables exist?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".tables"

# Does universe_members exist? (earlier got "no such table")
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT COUNT(*) FROM vanguard_universe_members
" 2>&1

# What about vanguard_health?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, status, COUNT(*) 
FROM vanguard_health 
GROUP BY asset_class, status 
ORDER BY asset_class, status
"
```

## Investigation 5: Alpaca adapter — what does it do now?

```bash
# What functions does the Alpaca adapter expose?
grep -n "^def \|^class \|^    def " \
  ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -30

# How does it get equity symbols to poll?
grep -n "symbols\|universe\|active_equity\|get_.*symbols" \
  ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -15

# What data_source value does it write?
grep -n "data_source\|alpaca_ws\|alpaca_rest" \
  ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -15
```

## Investigation 6: Twelve Data adapter — what does it do now?

```bash
# Entry points
grep -n "^def \|^class \|^    def " \
  ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py | head -30

# How does it get symbols?
grep -n "symbols\|config\|universe\|json" \
  ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py | head -15

# Credit budget / rate limiting
grep -n "credit\|rate.*limit\|sleep\|batch\|budget" \
  ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py | head -15
```

## Investigation 7: 1m → 5m aggregation

```bash
# Where does aggregation happen?
grep -rn "aggregate\|1m.*5m\|bars_1m.*bars_5m\|derive" \
  ~/SS/Vanguard/stages/ ~/SS/Vanguard/vanguard/ 2>/dev/null | head -15

# Is it inside the orchestrator or a separate function?
grep -n "aggregate\|5m" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -10
```

## OUTPUT FORMAT

Report everything as a structured summary:

```
=== V1/V2 ARCHITECTURE REPORT ===

1. ORCHESTRATOR V1 STEP:
   - What it does: [describe]
   - Alpaca: [REST poll / WebSocket / both]
   - Twelve Data: [inline poll / separate / not called]
   - Time: [Xs for Alpaca, Ys for Twelve Data]
   - Equity symbols source: [where does it get the list?]
   - Non-equity symbols source: [where?]

2. V2 PREFILTER:
   - Handles equities: [yes/no]
   - Handles non-equity: [yes/no]
   - Input table: [which table]
   - Output table: [which table]
   - How survivors reach V3: [describe]

3. EQUITY PREFILTER:
   - Location: [inside V1 / separate file / inside orchestrator / none]
   - How it works: [describe]
   - When it runs: [per-cycle / at startup / scheduled]

4. DATA FLOW:
   - Alpaca writes to: [table, data_source value]
   - Twelve Data writes to: [table, data_source value]
   - Aggregation: [where, when]
   - Tables that exist: [list]

5. KEY FILES:
   [list all relevant files with one-line description]

6. BOTTLENECK:
   [what takes the most time and why]
```

DO NOT FIX ANYTHING. Report only.
