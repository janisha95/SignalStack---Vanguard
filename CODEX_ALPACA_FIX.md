# CODEX — Fix Alpaca Equity Streaming + Orchestrator Performance

## Context
Vanguard intraday ran a full cycle during market hours. Forex scored fine but
ZERO equity bars are flowing. The Alpaca WebSocket adapter starts and exits
silently. Also the orchestrator takes 69 seconds per cycle — needs to be faster
for 5-minute intraday cycling.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-alpaca-fix"
```

---

## PROBLEM 1: Alpaca WebSocket adapter exits silently

The adapter has keys in `~/SS/.env`:
```
ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS
```

The adapter reads:
```python
# Line 90
self.api_key = api_key or os.environ.get("ALPACA_KEY") or os.environ.get("ALPACA_API_KEY")
# Line 383
self.api_key = api_key or os.environ.get("ALPACA_KEY") or os.environ.get("APCA_API_KEY_ID", "")
```

But `~/SS/.env` is NOT auto-loaded into the process environment.

### Debug steps:
```bash
# 1. Check if keys are in shell environment
echo "ALPACA_KEY=$ALPACA_KEY"

# 2. If empty, the .env isn't sourced. Check if dotenv loads it:
grep -n "dotenv\|load_dotenv\|\.env" ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -10

# 3. Run the adapter with visible output to see the actual error:
cd ~/SS/Vanguard
export ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
export ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS
python3 -c "
from vanguard.data_adapters.alpaca_adapter import AlpacaWebSocket
import asyncio
ws = AlpacaWebSocket()
print(f'API key loaded: {bool(ws.api_key)}')
print(f'API key prefix: {ws.api_key[:6] if ws.api_key else \"EMPTY\"}')
asyncio.run(ws.start())
" 2>&1 | head -30
```

### Fix:
Add .env loading at the top of `alpaca_adapter.py`:

```python
from pathlib import Path
from dotenv import load_dotenv

# Load keys from multiple .env locations
load_dotenv(Path.home() / 'SS' / '.env', override=False)
load_dotenv(Path.home() / 'SS' / '.env.shared', override=False)
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env', override=False)
```

If `python-dotenv` is not installed:
```bash
pip install python-dotenv --break-system-packages
```

### Also fix: The WebSocket needs a __main__ block or a run script

Check how the adapter is started:
```bash
grep -n "if __name__\|def main\|async def run\|def start" ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -10
```

If there's no `if __name__ == "__main__"` block, add one:
```python
if __name__ == "__main__":
    import asyncio
    ws = AlpacaWebSocket()
    print(f"Starting Alpaca WebSocket for {len(ws.symbols)} symbols...")
    asyncio.run(ws.start())
```

### Also check: Is the WebSocket supposed to run inside the orchestrator?

```bash
grep -n "alpaca\|websocket\|ws\|AlpacaWeb" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -15
```

The WebSocket might need to be started AS PART of V1 (inside the orchestrator),
not as a separate process. Check how V1 collects data:

```bash
grep -n "def.*v1\|stage.*1\|data.*poll\|bars.*fetch\|alpaca.*bars\|REST.*bars" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

There are TWO possible architectures:
- **WebSocket (streaming)**: Separate background process writes bars continuously → orchestrator reads from DB
- **REST (polling)**: Orchestrator calls Alpaca REST API each cycle to fetch latest bars

Check which one the orchestrator actually uses. If it's REST-based, the WebSocket
is only for real-time updates and the REST client needs to be working.

If the orchestrator uses REST for V1:
```bash
grep -n "get_bars\|fetch_bars\|bars_request\|alpaca.*rest\|data_url\|stocks/bars" ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py | head -15
```

Fix whichever method the orchestrator actually calls.

### Verify Alpaca is working after fix:
```bash
# Test REST endpoint
cd ~/SS/Vanguard
export ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
export ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS
python3 -c "
from vanguard.data_adapters.alpaca_adapter import AlpacaRESTClient
c = AlpacaRESTClient()
print(f'Key loaded: {c.api_key[:6]}...')
bars = c.get_bars(['AAPL','NVDA','MSFT'], timeframe='5Min', limit=5)
print(f'Bars returned: {len(bars) if bars is not None else 0}')
if bars is not None and len(bars) > 0:
    print(bars.head())
"

# Check fresh equity bars in DB
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, COUNT(*), MAX(bar_ts_utc)
FROM vanguard_bars_5m
WHERE bar_ts_utc > datetime('now', '-1 hour')
GROUP BY data_source, asset_class
"
```

After fix, there should be equity rows from alpaca with timestamps from the current hour.

---

## PROBLEM 2: Orchestrator takes 69 seconds per cycle

For intraday 5-minute cycling, 69 seconds is too slow. Profile where time is spent:

```bash
# Add timing to each stage
cd ~/SS/Vanguard
python3 -c "
import time, sqlite3
from pathlib import Path

DB = str(Path.home() / 'SS/Vanguard/data/vanguard_universe.db')
con = sqlite3.connect(DB, timeout=30)

# Check how many rows in key tables
for table in ['vanguard_bars_5m', 'vanguard_bars_1m', 'vanguard_factor_matrix', 'vanguard_predictions', 'vanguard_health']:
    try:
        n = con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f'{table}: {n:,} rows')
    except:
        print(f'{table}: does not exist')
con.close()
"
```

Likely bottlenecks:
1. V1 data poll — Twelve Data rate limiting (55 credits/min)
2. V3 factor engine — computing 35 features for 50+ symbols
3. DB writes — 10GB SQLite file, slow I/O

Quick wins:
- If V1 is slow: batch the Twelve Data poll, skip symbols with fresh bars
- If V3 is slow: only recompute features for symbols with new bars since last cycle
- If DB is slow: add indexes, reduce table sizes, or split DBs

Check where time is spent in the orchestrator:
```bash
grep -n "elapsed\|time\|seconds\|duration\|took" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

The orchestrator should already log per-stage timings. If not, add them.

---

## PROBLEM 3: Ensure the orchestrator starts Alpaca data collection in V1

If V1 currently only polls Twelve Data and relies on a separate WebSocket process
for equities, that's fragile. The orchestrator should handle all data collection itself.

Check if the orchestrator has a V1 step that fetches Alpaca bars:
```bash
grep -A5 "def.*run_v1\|stage.*1\|v1_poll\|alpaca.*poll\|fetch_equity" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30
```

If V1 only calls Twelve Data, add an Alpaca REST poll step:
- Fetch 5m bars for all active equity symbols from Alpaca REST API
- Write to vanguard_bars_5m with data_source='alpaca_rest'
- This replaces the WebSocket dependency for the orchestrator

---

## Verification

After ALL fixes:
```bash
# Set keys
export ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
export ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS

# Run single cycle during market hours
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -40

# Check equity predictions exist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, COUNT(*), 
    SUM(CASE WHEN direction='LONG' THEN 1 ELSE 0 END) as longs,
    SUM(CASE WHEN direction='SHORT' THEN 1 ELSE 0 END) as shorts
FROM vanguard_predictions
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_predictions)
GROUP BY asset_class
"

# Check V6 approves some for TTP accounts
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account, status, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account, status
"
```

Expected: equity predictions exist with both LONG and SHORT, TTP accounts approve some.

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: Alpaca key loading + equity data flow in orchestrator"
```
