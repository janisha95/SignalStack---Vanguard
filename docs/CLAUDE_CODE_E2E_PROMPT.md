# CLAUDE CODE TASK: Vanguard E2E Multi-Asset Pipeline Test

## Context

All V1-V7 Vanguard stages have been refurbished and committed. GPT gap analyses addressed for Stage 1, 2-3, 5, 6, 7. V4A training backfill is running overnight on 2,676 equity symbols + 90 forex/commodities/crypto symbols. We need to validate the full pipeline works end-to-end, especially for forex/crypto/commodities which have NEVER been tested through V2→V3→V5→V6.

## Pre-read (mandatory)

```bash
# Read these files FIRST to understand the system
cat ~/SS/Vanguard/docs/Vanguard_CURRENT_STATE.md
head -100 ~/SS/Vanguard/stages/vanguard_orchestrator.py
head -100 ~/SS/Vanguard/stages/vanguard_prefilter.py
head -100 ~/SS/Vanguard/stages/vanguard_factor_engine.py
head -100 ~/SS/Vanguard/stages/vanguard_selection.py
head -100 ~/SS/Vanguard/stages/vanguard_risk_filters.py
```

## Step 1: Run E2E diagnostic test

```bash
cd ~/SS/Vanguard
python3 /path/to/vanguard_e2e_test.py 2>&1
```

If the script isn't available, run these checks manually:

```bash
# DB health
python3 -c "
import sqlite3
con = sqlite3.connect('/Users/sjani008/SS/Vanguard/data/vanguard_universe.db')
cur = con.cursor()

# Tables
tables = [r[0] for r in cur.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchall()]
print('Tables:', tables)

# Bar counts
for t in ['vanguard_bars_1m', 'vanguard_bars_5m', 'vanguard_bars_1h']:
    if t in tables:
        cnt = cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        syms = cur.execute(f'SELECT COUNT(DISTINCT symbol) FROM {t}').fetchone()[0]
        print(f'{t}: {cnt:,} rows, {syms} symbols')

# All symbols in 5m bars
syms = [r[0] for r in cur.execute('SELECT DISTINCT symbol FROM vanguard_bars_5m ORDER BY symbol').fetchall()]
print(f'All 5m symbols ({len(syms)}): {syms}')

# universe_members
if 'universe_members' in tables:
    um = cur.execute('SELECT asset_class, source, COUNT(*) FROM universe_members GROUP BY asset_class, source').fetchall()
    print('Universe members:', um)

# Training data progress
if 'vanguard_training_data' in tables:
    td = cur.execute('SELECT COUNT(*), COUNT(DISTINCT symbol) FROM vanguard_training_data').fetchone()
    print(f'Training data: {td[0]:,} rows, {td[1]} symbols')

# V4A still running?
import subprocess
r = subprocess.run(['pgrep', '-f', 'vanguard_training_backfill'], capture_output=True, text=True)
print(f'V4A running: {\"YES\" if r.returncode == 0 else \"NO\"}')

con.close()
"
```

## Step 2: Test V2 prefilter on multi-asset symbols

```bash
cd ~/SS/Vanguard
python3 stages/vanguard_prefilter.py --dry-run --verbose 2>&1
```

Expected: Should show forex/crypto/metal symbols being health-checked.
If it fails, read the error and investigate the actual code path.

## Step 3: Test V3 factor engine on specific multi-asset symbols

```bash
cd ~/SS/Vanguard
# Test with a mix of asset classes
python3 stages/vanguard_factor_engine.py --dry-run --symbols AAPL,EURUSD,XAUUSD,BTCUSD 2>&1
```

Expected: 35 features computed for each symbol. NaN rate < 50%.
If `--symbols` flag doesn't exist, check the actual CLI flags in the file.

## Step 4: Test V5 selection

```bash
cd ~/SS/Vanguard
# If V5 has a dry-run or debug mode
python3 stages/vanguard_selection.py --dry-run 2>&1
```

Check: Does V5 have strategy definitions for forex, crypto, metals, etc?
Read the actual code to find what strategies are registered per asset class.

## Step 5: Test V7 single cycle (DRY RUN first)

```bash
cd ~/SS/Vanguard
python3 stages/vanguard_orchestrator.py --single-cycle --dry-run 2>&1
```

If --dry-run works, try without it (execution defaults to OFF per spec):
```bash
python3 stages/vanguard_orchestrator.py --single-cycle 2>&1
```

## Step 6: Check V4A progress

```bash
# Tail the V4A log
tail -50 ~/SS/Vanguard/logs/v4a_training.log

# Check if still running
ps aux | grep vanguard_training_backfill | grep -v grep

# Row count
python3 -c "
import sqlite3
con = sqlite3.connect('/Users/sjani008/SS/Vanguard/data/vanguard_universe.db')
cnt = con.execute('SELECT COUNT(*), COUNT(DISTINCT symbol) FROM vanguard_training_data').fetchone()
print(f'Training rows: {cnt[0]:,}, Symbols: {cnt[1]}')
con.close()
"
```

## Reporting

After running all steps, report:
1. **Bar coverage**: Which asset classes have bars? How many symbols per class?
2. **V2 result**: Did prefilter run? How many multi-asset survivors?
3. **V3 result**: Did factors compute for forex/crypto/metal? NaN rates?
4. **V5 result**: Are strategy routers defined for all asset classes?
5. **V6 result**: Does account config exist for TTP + FTMO?
6. **V7 result**: Did single-cycle complete? Any errors?
7. **V4A progress**: How many rows/symbols complete? Is it still running?
8. **Blockers**: What prevents running a full live cycle during market hours?

## Rules
- Do NOT modify any production code
- Do NOT write to the production DB (use --dry-run flags)
- If a stage fails, READ THE ACTUAL ERROR before suggesting fixes
- Report exact error messages and stack traces
- If a CLI flag doesn't exist, read the file's argparse section to find the real flags
