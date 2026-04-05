# CODEX — Fix V1 Performance + IBKR Streaming + Monday Prep

## Context
IBKR adapter uses `reqHistoricalData` per symbol (165s for 200 symbols).
This is the WRONG approach. IBKR supports streaming with `keepUpToDate=True`
which subscribes ONCE and receives continuous updates — no pacing limits.

For RIGHT NOW: use Alpaca WS for equity (already streaming, 3s), IBKR for forex only.
For WEEKEND: rewrite IBKR to use streaming so it's fast for everything.

## Git backup
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-v1-perf-fix"
```

---

## IMMEDIATE FIX: Make V1 Fast for Next 2 Hours

### Step 1: Skip IBKR equity intraday polling

File: `~/SS/Vanguard/stages/vanguard_orchestrator.py`

Find where IBKR equity bars are polled in V1. It's the slow path (165s).
Make IBKR only poll FOREX during cycles, not equities:

```bash
grep -n "ibkr.*equity\|ibkr.*poll\|_poll_ibkr\|ibkr_bars\|IB_INTRADAY" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20
```

Change the IBKR polling to forex-only:

```python
# In the V1 IBKR polling step:
# BEFORE: polls all IBKR symbols (equity + forex) — 165s
# AFTER: polls forex only (14 pairs) — ~5s

# Get only forex symbols for IBKR polling
ibkr_forex_symbols = [s for s in ibkr_symbols if s.get('asset_class') == 'forex']
# Equities come from Alpaca WS (already streaming, instant)
```

### Step 2: Fix sector_cap bug

53/56 TTP rejections are `SECTOR_CAP:technology` because unknown equities
default to "technology" in V6's static sector map.

```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
UPDATE account_profiles SET max_per_sector = 50 WHERE id = 'TTP_20K_SWING';
"
```

Also fix the root cause in V6:
```bash
grep -n "sector\|technology\|default.*sector\|unknown.*sector" \
  ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -15
```

Change the default sector from "technology" to "unknown":
```python
# Find the line that defaults to "technology" and change to:
sector = candidate.get("sector") or "unknown"
```

And make sector_cap skip unknown sectors:
```python
if sector == "unknown":
    pass  # Don't apply sector cap to unclassified equities
```

### Step 3: Verify fast cycle

```bash
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -30

# Expected: V1 < 30s (Alpaca equity 3s + IBKR forex 5s + TD crypto 15s)
# Expected: V6 approves 10-20 candidates (sector_cap fixed)
```

---

## WEEKEND FIX: IBKR Streaming Adapter (permanent)

Rewrite the IBKR intraday bar fetching to use streaming instead of polling.

### Current (BROKEN — 165s per cycle):
```python
# For each symbol, one at a time:
bars = ib.reqHistoricalData(contract, '', barSizeSetting='5 mins', ...)
# 200 symbols × ~0.8s each = 160+ seconds
```

### New (CORRECT — <1s per cycle):
```python
# AT STARTUP (once):
for contract in valid_contracts:
    bars = ib.reqHistoricalData(
        contract, '',
        barSizeSetting='5 mins',
        durationStr='3600 S',
        whatToShow='TRADES',  # or 'MIDPOINT' for forex
        useRTH=False,         # include extended hours
        keepUpToDate=True,    # STREAMING — receives updates automatically
        formatDate=2
    )
    # Register callback for new bars
    bars.updateEvent += lambda bars, hasNewBar, contract=contract: on_bar_update(contract, bars, hasNewBar)

# CALLBACK (fires automatically when new bar arrives):
def on_bar_update(contract, bars, hasNewBar):
    if hasNewBar:
        latest = bars[-1]
        write_bar_to_db(contract.symbol, latest)

# PER CYCLE (V1 step):
# Just read latest bars from DB — instant (<0.5s)
```

### Implementation in ibkr_adapter.py:

```bash
cat ~/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py | head -30
```

Add a new class or method:

```python
class IBKRStreamingAdapter:
    """
    Streaming IBKR data adapter.
    Subscribes to historical bars with keepUpToDate=True at startup.
    Bars arrive via callbacks and are written to DB continuously.
    V1 cycle just reads latest bars from DB (instant).
    """
    
    def __init__(self, host='127.0.0.1', port=4001, client_id=10):
        import asyncio
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        
        from ib_insync import IB
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self.subscriptions = {}  # symbol -> BarDataList
        self.valid_contracts = {}  # symbol -> Contract
        self.bad_symbols = set()  # symbols that failed qualification
    
    def connect(self):
        self.ib.connect(self.host, self.port, self.client_id)
        return self.ib.isConnected()
    
    def qualify_contracts(self, symbols: list, asset_class='equity'):
        """
        Qualify all contracts in BULK at startup.
        Cache valid ones, blacklist bad ones.
        This runs ONCE, not per cycle.
        """
        from ib_insync import Stock, Forex
        
        contracts = []
        for sym in symbols:
            if asset_class == 'forex':
                c = Forex(sym)
            else:
                c = Stock(sym, 'SMART', 'USD')
            contracts.append(c)
        
        # Bulk qualify — much faster than one-by-one
        qualified = self.ib.qualifyContracts(*contracts)
        
        for c in qualified:
            if c.conId and c.conId > 0:
                self.valid_contracts[c.symbol] = c
            else:
                self.bad_symbols.add(c.symbol)
        
        logger.info(f"Qualified {len(self.valid_contracts)} contracts, "
                     f"{len(self.bad_symbols)} bad symbols")
    
    def subscribe_streaming(self, db_path: str, use_rth=False):
        """
        Subscribe to keepUpToDate=True for all valid contracts.
        Bars arrive via callback and are written to DB.
        """
        import sqlite3
        
        for sym, contract in self.valid_contracts.items():
            what_to_show = 'MIDPOINT' if isinstance(contract, Forex) else 'TRADES'
            
            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='3600 S',
                barSizeSetting='1 min',
                whatToShow=what_to_show,
                useRTH=use_rth,
                keepUpToDate=True,
                formatDate=2
            )
            
            # Register callback
            bars.updateEvent += lambda bars, hasNewBar, s=sym: \
                self._on_bar(s, bars, hasNewBar, db_path)
            
            self.subscriptions[sym] = bars
        
        logger.info(f"Streaming {len(self.subscriptions)} symbols")
    
    def _on_bar(self, symbol, bars, hasNewBar, db_path):
        """Callback fired when new bar arrives."""
        if not hasNewBar or not bars:
            return
        
        latest = bars[-1]
        # Write to DB
        import sqlite3
        con = sqlite3.connect(db_path, timeout=10)
        con.execute(
            "INSERT OR REPLACE INTO ibkr_bars_1m "
            "(symbol, bar_ts_utc, open, high, low, close, volume, asset_class, data_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ibkr')",
            (symbol, str(latest.date), latest.open, latest.high,
             latest.low, latest.close, int(latest.volume),
             'forex' if '/' in symbol else 'equity')
        )
        con.commit()
        con.close()
    
    def get_latest_bars(self, db_path: str, minutes=10):
        """
        Read latest bars from DB. Called by V1 each cycle.
        This is INSTANT — just a DB read.
        """
        import sqlite3
        con = sqlite3.connect(db_path, timeout=10)
        bars = pd.read_sql(
            "SELECT * FROM ibkr_bars_1m "
            "WHERE bar_ts_utc > datetime('now', ?)",
            con, params=(f'-{minutes} minutes',)
        )
        con.close()
        return bars
```

### Key differences from current adapter:
1. `qualifyContracts(*contracts)` — BULK qualification at startup (not per-symbol)
2. `keepUpToDate=True` — bars stream in via callback (no polling)
3. V1 cycle just reads DB — instant (<0.5s)
4. Bad symbols blacklisted at startup — no retries
5. `useRTH=False` — includes pre-market + after-hours data

### Wire into orchestrator:

```python
# At startup:
self.ibkr_streaming = IBKRStreamingAdapter()
self.ibkr_streaming.connect()
self.ibkr_streaming.qualify_contracts(equity_symbols, 'equity')
self.ibkr_streaming.qualify_contracts(forex_symbols, 'forex')
self.ibkr_streaming.subscribe_streaming(IBKR_INTRADAY_DB, use_rth=False)

# Per cycle V1:
# Just read latest bars from DB (instant)
latest_bars = self.ibkr_streaming.get_latest_bars(IBKR_INTRADAY_DB, minutes=10)
```

---

## ALSO: Verify all fixes together

```bash
# Single cycle test
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -30

# Check stage timings (V1 should be <30s)
# Check V6 approvals (should be 10-20, not 4)

# Check shortlist
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, direction, ROUND(edge_score, 4) as edge, rank
FROM vanguard_shortlist_v2
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist_v2)
ORDER BY direction, rank
LIMIT 20
"

# Check V6 approvals
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account_id, status
"

# Start loop for 2 hours
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 2>&1 | tee ~/SS/logs/vanguard_loop.log &
```

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "fix: V1 perf — Alpaca equity + IBKR forex streaming + sector_cap"
```
