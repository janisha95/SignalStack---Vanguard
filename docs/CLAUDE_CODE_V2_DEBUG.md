# CLAUDE CODE TASK: Fix V2 Health Classification — LOW_VOLUME + HALTED Killing All Non-Equity Symbols

## Context

The Twelve Data adapter bug is fixed (commit `498986b`). Adapter now writes 49-60 bars per poll. But V2 still produces 0 survivors:

```
ACTIVE: 0 | STALE: 1 | LOW_VOLUME: 53 | HALTED: 36 | CLOSED: 215
```

The 215 CLOSED are equities (market closed) — correct.
The 53 LOW_VOLUME and 36 HALTED are forex/crypto/metals — these markets are OPEN and should be ACTIVE.

**This is the last blocker before V3→V5→V6 can run on multi-asset data for the first time.**

## Step 1: Read V2 code thoroughly

```bash
cat ~/SS/Vanguard/stages/vanguard_prefilter.py
```

Understand:
- How does V2 classify symbols? What are the 5 health checks?
- What volume field does it check? (`volume`, `tick_volume`, `trade_count`?)
- What are the actual threshold values per asset class?
- How does it determine HALTED status?
- Does it know the asset class of each symbol? Where does it read that from?

## Step 2: Check what volume data actually exists in bars

```bash
# What columns exist in 5m bars?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_bars_5m)"

# What columns exist in 1m bars?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_bars_1m)"

# Sample recent bars for a forex symbol — what does the volume field look like?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, timestamp, open, close, volume
FROM vanguard_bars_5m
WHERE symbol = 'EUR/USD'
ORDER BY timestamp DESC
LIMIT 10
"

# Same for crypto
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, timestamp, open, close, volume
FROM vanguard_bars_5m
WHERE symbol = 'BTC/USD'
ORDER BY timestamp DESC
LIMIT 10
"

# Same for gold
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, timestamp, open, close, volume
FROM vanguard_bars_5m
WHERE symbol = 'XAU/USD'
ORDER BY timestamp DESC
LIMIT 10
"

# Check if there's a tick_volume or trade_count column
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, timestamp, *
FROM vanguard_bars_5m
WHERE symbol = 'EUR/USD'
ORDER BY timestamp DESC
LIMIT 3
"
```

## Step 3: Check HALTED classification logic

```bash
# What makes V2 mark something HALTED?
grep -n "HALTED\|halted\|halt" ~/SS/Vanguard/stages/vanguard_prefilter.py

# Which symbols are HALTED?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, status
FROM vanguard_health
WHERE status = 'HALTED'
ORDER BY asset_class, symbol
"

# Compare: which symbols are in universe_members but NOT in recent bars?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT um.symbol, um.asset_class, um.data_source,
       (SELECT MAX(timestamp) FROM vanguard_bars_5m WHERE symbol = um.symbol) as latest_bar
FROM universe_members um
WHERE um.is_active = 1
AND um.data_source LIKE '%twelve%'
ORDER BY latest_bar IS NULL DESC, um.asset_class, um.symbol
LIMIT 50
"
```

## Step 4: Check volume thresholds vs actual values

```bash
# What volume thresholds does V2 use?
grep -n "volume_threshold\|min_volume\|LOW_VOLUME\|tick_volume" ~/SS/Vanguard/stages/vanguard_prefilter.py

# What are actual volume values for Twelve Data symbols?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol,
       AVG(volume) as avg_vol,
       MAX(volume) as max_vol,
       MIN(volume) as min_vol,
       COUNT(*) as bars
FROM vanguard_bars_5m
WHERE symbol IN (
    SELECT symbol FROM universe_members
    WHERE data_source LIKE '%twelve%'
    LIMIT 20
)
AND timestamp > datetime('now', '-2 hours')
GROUP BY symbol
ORDER BY avg_vol
"

# If volume is 0 or NULL for all Twelve Data bars, that's the problem
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT
    CASE WHEN volume IS NULL THEN 'NULL'
         WHEN volume = 0 THEN 'ZERO'
         WHEN volume > 0 THEN 'POSITIVE'
    END as vol_type,
    COUNT(*) as cnt
FROM vanguard_bars_5m
WHERE symbol IN (SELECT symbol FROM universe_members WHERE data_source LIKE '%twelve%')
AND timestamp > datetime('now', '-24 hours')
GROUP BY vol_type
"
```

## Step 5: Check asset class mapping

```bash
# Does V2 know which symbols are forex vs crypto vs metal?
# How does it look up asset class?
grep -n "asset_class\|universe_members\|get_asset" ~/SS/Vanguard/stages/vanguard_prefilter.py | head -20

# What asset classes are in universe_members for Twelve Data symbols?
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*) FROM universe_members
WHERE data_source LIKE '%twelve%' AND is_active = 1
GROUP BY asset_class
"
```

## Step 6: Diagnose and fix

The likely issues are:

### If volume is 0 or NULL for Twelve Data bars:
- Twelve Data returns tick volume differently — might be in a `trade_count` field
- Or the adapter isn't mapping the volume field from the API response
- Fix: map the correct field, or use tick_volume for non-equity symbols

### If volume thresholds are too high for tick volume:
- Equity real volume: millions of shares → threshold 200,000 makes sense
- Forex tick volume: might be 50-500 ticks per 5m bar
- The V2 spec says per-asset-class thresholds: forex=50, crypto=10, metal=20
- But the actual code might use a single threshold or the wrong field
- Fix: ensure V2 reads per-asset-class thresholds and applies them correctly

### If HALTED is caused by missing bars (not a real halt):
- If a symbol has no bars in the last N minutes, V2 might mark it HALTED
- But Twelve Data only polls once per cycle, so some symbols might not have fresh bars
- Fix: HALTED should mean "exchange halt flag", not "no recent bars" (that should be STALE)

### If asset_class lookup fails:
- V2 might default to equity thresholds for all symbols
- Equity volume threshold applied to forex tick volume = instant LOW_VOLUME
- Fix: ensure V2 reads asset_class from universe_members for each symbol

## Step 7: After fixing, re-test

```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1
```

Expected after fix:
- Forex symbols (EUR/USD, GBP/USD etc): ACTIVE
- Crypto symbols (BTC/USD, ETH/USD etc): ACTIVE
- Metal symbols (XAU/USD, XAG/USD): ACTIVE
- V3 should run and compute features
- V5 should route to per-asset-class strategies
- V6 should size positions

If V5 produces 0 shortlist because models don't exist yet (V4B hasn't run), that's expected and OK — the important thing is that V3 runs and V5 logs "NO_MODELS" explicitly rather than silently returning empty.

## Reporting

```
## V2 Debug Results

### Root cause: [describe what you found]

### Volume field mapping
- Column used by V2: [volume / tick_volume / other]
- Actual values for forex 5m bars: [range]
- Actual values for crypto 5m bars: [range]
- Threshold applied: [value per asset class]

### HALTED classification
- Cause: [missing bars / exchange flag / other]
- Symbols affected: [count by asset class]

### Fix applied: [describe]
### Commit: [hash]

### Re-test (orchestrator --single-cycle)
- V2 survivors: X ACTIVE (Y forex, Z crypto, W metal)
- V3 ran: YES/NO — features for X symbols
- V5 ran: YES/NO — shortlist X rows (or NO_MODELS logged)
- V6 ran: YES/NO — portfolio X rows
```

## Rules
- Read V2 code FIRST — understand the actual logic before changing anything
- Git backup: `cd ~/SS/Vanguard && git add -A && git stash` before changes
- Do NOT weaken thresholds blindly — understand what volume values actually exist
- Do NOT change V2 to skip volume checks — fix the data mapping or thresholds to match reality
- The V2 spec defines per-asset-class thresholds (forex=50, crypto=10, metal=20 tick volume) — respect those
- If volume is genuinely 0 for Twelve Data bars, the fix is in the adapter's field mapping, not in V2
