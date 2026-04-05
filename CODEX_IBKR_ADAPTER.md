# CODEX — IBKR Adapter: Universal Data Service for SignalStack

## Context

IBKR connection is LIVE and tested. IB Gateway running on localhost:4001.
Free data available: IDEALPRO FX (forex), PAXOS (crypto), IBKR-PRO (US equities via IEX).
This adapter serves BOTH the daily system (Meridian) and intraday system (Vanguard).

Connection test confirmed:
- AAPL 5m bars: 4 bars returned, C=$254.51
- EURUSD 5m bars: 7 bars returned, C=1.15427  
- NVDA 5m bars: 4 bars returned, C=$176.37

Python 3.14 requires: `asyncio.set_event_loop(asyncio.new_event_loop())` before importing ib_insync.

## Git backup
```bash
cd ~/SS && git add -A && git commit -m "backup: pre-ibkr-adapter" 2>/dev/null
cd ~/SS/Vanguard && git add -A && git commit -m "backup: pre-ibkr-adapter"
cd ~/SS/Meridian && git add -A && git commit -m "backup: pre-ibkr-adapter"
```

---

## Architecture

```
                     ibkr_adapter.py
                    (shared library)
                          │
     ┌────────────────────┼────────────────────┐
     │                    │                    │
 INTRADAY PATH       DAILY PATH          FOREX/CRYPTO PATH
 (Vanguard)          (S1 + Meridian)     (Vanguard)
     │                    │                    │
 Equity Prefilter    NO prefilter         NO prefilter
 12K → 1,500-2K     Pull ALL ~3,000+     Pull ALL forex pairs
     │                    │                    │
 Stream 1m/5m       Pull daily OHLCV     Stream 1m/5m
 bars via WS        once per evening     bars via WS
     │                    │                    │
 ibkr_intraday.db   ibkr_daily.db        ibkr_intraday.db
 (Vanguard only)    (S1 + Meridian       (Vanguard only)
                     SHARED — one DB,
                     both systems read)
```

**KEY RULES:**
- Prefilter applies to INTRADAY EQUITIES ONLY. Daily bars get the full universe.
- S1 and Meridian SHARE ibkr_daily.db — one source of truth for daily bars.
- S1 stops using S8/Alpaca/yfinance for data. Reads from ibkr_daily.db instead.

---

## Step 0: Read existing code first

```bash
# Understand current adapter patterns
head -50 ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py
head -50 ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py

# Understand how universe_members works
grep -n "universe_members\|vanguard_universe_members" \
  ~/SS/Vanguard/vanguard/helpers/universe_builder.py | head -20

# Current DB schema for bars
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_bars_1m)"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "PRAGMA table_info(vanguard_bars_5m)"

# How orchestrator calls V1
grep -n "alpaca\|twelve\|poll\|adapter\|v1" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30

# Check ib_insync is installed
python3 -c "
import asyncio; asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import IB
print('ib_insync OK')
"
```

---

## Step 1: Create TWO new databases

### ibkr_intraday.db (for Vanguard intraday — 1m and 5m bars)

```bash
python3 -c "
import sqlite3
from pathlib import Path

db = str(Path.home() / 'SS/Vanguard/data/ibkr_intraday.db')
con = sqlite3.connect(db)
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA busy_timeout=30000')

con.execute('''CREATE TABLE IF NOT EXISTS ibkr_bars_1m (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER DEFAULT 0,
    tick_volume INTEGER DEFAULT 0,
    asset_class TEXT DEFAULT 'equity',
    data_source TEXT DEFAULT 'ibkr',
    PRIMARY KEY (symbol, bar_ts_utc)
)''')

con.execute('''CREATE TABLE IF NOT EXISTS ibkr_bars_5m (
    symbol TEXT NOT NULL,
    bar_ts_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER DEFAULT 0,
    tick_volume INTEGER DEFAULT 0,
    bar_count INTEGER DEFAULT 5,
    asset_class TEXT DEFAULT 'equity',
    data_source TEXT DEFAULT 'ibkr',
    PRIMARY KEY (symbol, bar_ts_utc)
)''')

con.execute('''CREATE TABLE IF NOT EXISTS ibkr_universe (
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    exchange TEXT,
    currency TEXT DEFAULT 'USD',
    con_id INTEGER,
    min_tick REAL,
    lot_size REAL,
    is_active INTEGER DEFAULT 1,
    last_seen TEXT,
    PRIMARY KEY (symbol, asset_class)
)''')

con.execute('CREATE INDEX IF NOT EXISTS idx_ib1m_ts ON ibkr_bars_1m(bar_ts_utc)')
con.execute('CREATE INDEX IF NOT EXISTS idx_ib5m_ts ON ibkr_bars_5m(bar_ts_utc)')
con.execute('CREATE INDEX IF NOT EXISTS idx_ib1m_sym ON ibkr_bars_1m(symbol)')
con.commit()
con.close()
print(f'Created {db}')
"
```

### ibkr_daily.db (for Meridian daily — daily OHLCV bars)

```bash
python3 -c "
import sqlite3
from pathlib import Path

db = str(Path.home() / 'SS/Meridian/data/ibkr_daily.db')
con = sqlite3.connect(db)
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA busy_timeout=30000')

con.execute('''CREATE TABLE IF NOT EXISTS ibkr_daily_bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER DEFAULT 0,
    avg_volume_20d REAL,
    dollar_volume REAL,
    asset_class TEXT DEFAULT 'equity',
    data_source TEXT DEFAULT 'ibkr',
    PRIMARY KEY (symbol, date)
)''')

con.execute('''CREATE TABLE IF NOT EXISTS ibkr_daily_universe (
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    exchange TEXT,
    currency TEXT DEFAULT 'USD',
    con_id INTEGER,
    market_cap REAL,
    sector TEXT,
    industry TEXT,
    is_active INTEGER DEFAULT 1,
    last_seen TEXT,
    PRIMARY KEY (symbol)
)''')

con.execute('CREATE INDEX IF NOT EXISTS idx_ibd_date ON ibkr_daily_bars(date)')
con.execute('CREATE INDEX IF NOT EXISTS idx_ibd_sym ON ibkr_daily_bars(symbol)')
con.commit()
con.close()
print(f'Created {db}')
"
```

---

## Step 2: Create ibkr_adapter.py (shared library)

Create: `~/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py`

This adapter handles ALL IBKR data operations for both systems.

### Core Design:

```python
#!/usr/bin/env python3
"""
ibkr_adapter.py — Universal IBKR data adapter for SignalStack.

Serves both:
- Vanguard intraday (1m/5m streaming bars for equities + forex)
- Meridian daily (daily OHLCV for full equity universe)

Connection: IB Gateway on localhost:4001
Free data: IDEALPRO FX, PAXOS Crypto, IBKR-PRO (IEX equities)

Usage:
    from vanguard.data_adapters.ibkr_adapter import IBKRAdapter
    
    adapter = IBKRAdapter()
    adapter.connect()
    
    # Intraday: stream 5m bars for filtered equities
    bars = adapter.get_intraday_bars(['AAPL','NVDA'], timeframe='5m', limit=10)
    
    # Daily: pull full universe daily bars (no prefilter)
    bars = adapter.get_daily_bars(['AAPL','NVDA'], days=5)
    
    # Forex: stream via IDEALPRO
    bars = adapter.get_forex_bars(['EURUSD','GBPUSD'], timeframe='5m', limit=10)
    
    # Equity universe: get all tradeable US equities
    symbols = adapter.get_equity_universe()  # ~12,000 symbols
    
    # Prefiltered: apply intraday filters
    filtered = adapter.get_intraday_equity_universe()  # ~1,500-2,000
"""
```

### Key methods the adapter must have:

```python
class IBKRAdapter:
    def __init__(self, host='127.0.0.1', port=4001, client_id=10):
        """Initialize. Use client_id=10 to avoid conflicts with other clients."""
    
    def connect(self) -> bool:
        """Connect to IB Gateway. Handle Python 3.14 asyncio fix."""
    
    def disconnect(self):
        """Clean disconnect."""
    
    def is_connected(self) -> bool:
        """Check connection status."""
    
    # ── UNIVERSE ──────────────────────────────────────────────
    
    def get_full_equity_universe(self) -> list[dict]:
        """
        Get ALL tradeable US equities from IBKR.
        Returns ~12,000 symbols with metadata (exchange, conId, etc.)
        Used by: Meridian daily (no prefilter applied here)
        """
    
    def get_intraday_equity_universe(self) -> list[str]:
        """
        Apply intraday prefilter to equity universe:
        - Price > $5
        - Avg daily dollar volume > $5M  
        - Market cap > $300M
        - No ETFs, ADRs, SPACs, warrants, units, rights
        - Listed on NYSE, NASDAQ, AMEX only
        Returns ~1,500-2,000 symbols
        Used by: Vanguard intraday ONLY
        """
    
    def get_forex_pairs(self) -> list[str]:
        """
        Get available forex pairs from IDEALPRO.
        Returns: ['EURUSD', 'GBPUSD', 'USDJPY', ...]
        """
    
    def get_crypto_symbols(self) -> list[str]:
        """
        Get available crypto from PAXOS.
        Returns: ['BTC', 'ETH', 'LTC', 'BCH']
        """
    
    # ── INTRADAY BARS (for Vanguard) ──────────────────────────
    
    def get_bars(self, symbols: list, timeframe='5m', limit=10,
                 asset_class='equity') -> dict:
        """
        Fetch recent bars for a list of symbols.
        
        For equities: Stock('SYM', 'SMART', 'USD'), whatToShow='TRADES'
        For forex: Forex('EURUSD'), whatToShow='MIDPOINT'
        For crypto: Crypto('BTC', 'PAXOS', 'USD'), whatToShow='TRADES'
        
        Returns: {symbol: [bar_dicts]} where each bar_dict has:
            ts_utc, open, high, low, close, volume
        
        IMPORTANT: IBKR has pacing rules:
        - Max 60 historical data requests per 10 minutes
        - For streaming, use reqRealTimeBars or reqMktData instead
        """
    
    def stream_bars(self, symbols: list, callback, asset_class='equity'):
        """
        Start streaming real-time 5-second bars via reqRealTimeBars.
        Aggregate into 1m bars internally, write to DB.
        
        callback(symbol, bar_dict) called for each completed 1m bar.
        
        OR use reqHistoricalData with keepUpToDate=True for 5m streaming.
        """
    
    # ── DAILY BARS (for Meridian) ─────────────────────────────
    
    def get_daily_bars(self, symbols: list, days=5) -> dict:
        """
        Fetch daily OHLCV bars.
        NO prefilter — pull for ALL requested symbols.
        
        whatToShow='TRADES', barSizeSetting='1 day'
        
        Returns: {symbol: [bar_dicts]} where each bar_dict has:
            date, open, high, low, close, volume
        """
    
    # ── DB WRITERS ────────────────────────────────────────────
    
    def write_intraday_bars(self, bars: dict, db_path: str):
        """Write bars to ibkr_intraday.db."""
    
    def write_daily_bars(self, bars: dict, db_path: str):
        """Write bars to ibkr_daily.db."""
```

### Python 3.14 compatibility:
```python
import asyncio
# MUST be before importing ib_insync
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Forex, Crypto, Contract
```

### IBKR pacing rules (CRITICAL):
```
- Max 60 historical data requests per 10 minutes
- Max 6 requests per 2 seconds for the same contract
- Identical requests within 15 seconds are blocked
- For bulk data: batch requests, sleep between batches
- For real-time: use keepUpToDate=True (no pacing limit)
```

### Equity prefilter implementation:

```python
def get_intraday_equity_universe(self) -> list[str]:
    """
    Filter IBKR's full equity list for intraday trading.
    
    Step 1: Request scanner for US equities (scannerSubscription)
            OR use reqMatchingSymbols for known exchanges
    Step 2: Filter by:
        - exchange in ('NYSE', 'NASDAQ', 'AMEX', 'ARCA')
        - secType = 'STK' (not ETF, WAR, etc.)
        - price > $5 (from latest snapshot)
        - avg daily volume > 1M shares or dollar_volume > $5M
    Step 3: Exclude:
        - ETFs (use the etf_tickers.json from Meridian config)
        - Suffix exclusions: .WS, .WT, .U, .R (warrants, units, rights)
        - Known SPACs
    Step 4: Return ~1,500-2,000 symbols
    
    Cache result for 30 minutes (prefilter doesn't need to run every cycle).
    """
```

### Connection to IB Gateway:

```python
IB_HOST = '127.0.0.1'
IB_PORT = 4001          # IB Gateway port (not TWS 7497)
IB_CLIENT_ID = 10       # Unique per adapter instance

# Load from env as fallback
import os
IB_HOST = os.environ.get('IB_HOST', '127.0.0.1')
IB_PORT = int(os.environ.get('IB_PORT', '4001'))
```

---

## Step 3: Wire into Vanguard orchestrator

The orchestrator's V1 step needs a new IBKR path alongside Alpaca and Twelve Data.

```bash
grep -n "alpaca\|twelve\|poll\|adapter" \
  ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -30
```

Add IBKR as a data source in V1:

```python
# In orchestrator V1 step, add IBKR polling:
if self.ibkr_adapter and self.ibkr_adapter.is_connected():
    # Equity bars (prefiltered universe)
    equity_symbols = self.ibkr_adapter.get_intraday_equity_universe()
    equity_bars = self.ibkr_adapter.get_bars(equity_symbols, '5m', limit=5)
    self.ibkr_adapter.write_intraday_bars(equity_bars, IBKR_INTRADAY_DB)
    
    # Forex bars (all IDEALPRO pairs — replaces Twelve Data for forex)
    forex_pairs = self.ibkr_adapter.get_forex_pairs()
    forex_bars = self.ibkr_adapter.get_bars(forex_pairs, '5m', limit=5, asset_class='forex')
    self.ibkr_adapter.write_intraday_bars(forex_bars, IBKR_INTRADAY_DB)
```

### Data source priority:
1. **Equities**: IBKR (primary) → Alpaca (fallback if IBKR disconnected)
2. **Forex**: IBKR IDEALPRO (primary) → Twelve Data (fallback)
3. **Crypto**: Twelve Data (primary, more symbols) → PAXOS (BTC/ETH/LTC/BCH only)

### Orchestrator reads from ibkr_intraday.db for V2+:
V2 (prefilter/health), V3 (factors), V4B (scorer), V5 (selection) should
read from `ibkr_intraday.db` when IBKR is the active source.

---

## Step 4: Wire into Meridian (daily bars)

Create a simple daily bar fetcher that Meridian's Stage 1 cache can call:

```bash
# Check how Meridian currently fetches daily bars
grep -n "alpaca\|yfinance\|daily.*bars\|cache.*warm\|fetch" \
  ~/SS/Meridian/stages/v2_cache_warm.py | head -20
```

Add an IBKR option for Meridian's daily bar fetch:

```python
# In Meridian cache warm, add IBKR daily path:
from vanguard.data_adapters.ibkr_adapter import IBKRAdapter

def fetch_daily_bars_ibkr(symbols: list, days=5):
    """Fetch daily bars from IBKR for Meridian. NO prefilter."""
    adapter = IBKRAdapter(client_id=11)  # Different client_id from Vanguard
    adapter.connect()
    bars = adapter.get_daily_bars(symbols, days=days)
    adapter.write_daily_bars(bars, IBKR_DAILY_DB)
    adapter.disconnect()
    return bars
```

---

## Step 5: Register in universe_members

When IBKR adapter initializes, register its symbols in `vanguard_universe_members`:

```python
def register_universe(self, db_path):
    """Register IBKR symbols in vanguard_universe_members."""
    con = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    
    # Equity symbols (prefiltered for intraday)
    for sym in self.get_intraday_equity_universe():
        con.execute("""
            INSERT OR REPLACE INTO vanguard_universe_members
            (symbol, asset_class, data_source, universe, is_active, last_seen_at)
            VALUES (?, 'equity', 'ibkr', 'ibkr_equities', 1, ?)
        """, (sym, now))
    
    # Forex pairs
    for pair in self.get_forex_pairs():
        con.execute("""
            INSERT OR REPLACE INTO vanguard_universe_members
            (symbol, asset_class, data_source, universe, is_active, last_seen_at)
            VALUES (?, 'forex', 'ibkr', 'ibkr_forex', 1, ?)
        """, (pair, now))
    
    con.commit()
    con.close()
```

---

## Step 6: Verification

```bash
# Test adapter standalone
python3 -c "
import asyncio; asyncio.set_event_loop(asyncio.new_event_loop())
import sys; sys.path.insert(0, '/Users/sjani008/SS/Vanguard')
from vanguard.data_adapters.ibkr_adapter import IBKRAdapter

a = IBKRAdapter()
a.connect()
print(f'Connected: {a.is_connected()}')

# Equity bars
bars = a.get_bars(['AAPL','NVDA','MSFT','TSLA','AMZN'], '5m', limit=5)
for sym, b in bars.items():
    print(f'  {sym}: {len(b)} bars, latest C={b[-1][\"close\"] if b else \"N/A\"}')

# Forex bars  
fx = a.get_bars(['EURUSD','GBPUSD','USDJPY'], '5m', limit=5, asset_class='forex')
for sym, b in fx.items():
    print(f'  {sym}: {len(b)} bars')

# Universe size
print(f'Full equity universe: {len(a.get_full_equity_universe())}')
print(f'Intraday filtered: {len(a.get_intraday_equity_universe())}')
print(f'Forex pairs: {len(a.get_forex_pairs())}')

a.disconnect()
"

# Check DBs were created
ls -la ~/SS/Vanguard/data/ibkr_intraday.db
ls -la ~/SS/Meridian/data/ibkr_daily.db

# Check tables
sqlite3 ~/SS/Vanguard/data/ibkr_intraday.db ".tables"
sqlite3 ~/SS/Meridian/data/ibkr_daily.db ".tables"
```

---

## Step 7: Test with orchestrator

```bash
# Run single Vanguard cycle with IBKR data
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -30

# Check if IBKR bars are in the DB
sqlite3 ~/SS/Vanguard/data/ibkr_intraday.db "
SELECT asset_class, COUNT(*), COUNT(DISTINCT symbol), MAX(bar_ts_utc)
FROM ibkr_bars_5m
GROUP BY asset_class
"
```

---

## Step 8: Wire S1 to use ibkr_daily.db (replace S8/Alpaca/yfinance)

S1 currently fetches daily bars via:
- `~/SS/Advance/fast_universe_cache.py` (Alpaca REST → s1_standings.db)
- `~/SS/Advance/yf_cache_pipeline.py` (yfinance fallback)

This is the OLD S8 architecture. Replace with IBKR daily bars.

### Read current S1 data flow:
```bash
# How does S1 get its data?
grep -n "fast_universe_cache\|yf_cache\|s1_standings\|alpaca\|yfinance\|bars\|cache" \
  ~/SS/Advance/s1_evening_orchestrator.py | head -20

# What DB does S1 read from?
grep -n "s1_standings\|signalstack_results\|db_path\|DB_PATH\|sqlite" \
  ~/SS/Advance/agent_server.py | head -20

# What does S1's data look like?
sqlite3 ~/SS/Advance/data_cache/s1_standings.db "
SELECT COUNT(*), COUNT(DISTINCT ticker), MAX(date) FROM standings
"
```

### The change:
S1's evening scan needs daily OHLCV bars for ~3,000+ tickers.
Instead of Alpaca REST + yfinance, it should read from `ibkr_daily.db`.

Two options:
A) S1 reads directly from `~/SS/Meridian/data/ibkr_daily.db` (shared with Meridian)
B) S1 has its own copy at `~/SS/Advance/data_cache/ibkr_daily.db`

**Go with Option A** — one source of truth. S1 and Meridian both read from the same
daily bars. The IBKR adapter writes daily bars once, both systems consume.

### Wire S1's FUC (fast_universe_cache) to read from ibkr_daily.db:

```bash
# Find where S1 loads bar data
grep -rn "def.*load_bars\|def.*fetch_data\|def.*get_bars\|standings.*select\|SELECT.*close\|SELECT.*open" \
  ~/SS/Advance/agent_server.py ~/SS/Advance/fast_universe_cache.py | head -20
```

Create a thin wrapper or modify `fast_universe_cache.py` to read from `ibkr_daily.db`
instead of calling Alpaca REST. The table schema in ibkr_daily.db matches what S1 needs:
symbol, date, open, high, low, close, volume.

### NO prefilter for S1 daily:
S1 scans the full universe (~3,000+ tickers). The IBKR adapter pulls daily bars
for ALL equities without any prefilter. S1's own strategies + RF gate handle filtering.

### Test:
```bash
# After wiring, run S1 evening scan
cd ~/SS/Advance && python3 s1_evening_orchestrator.py 2>&1 | tail -20

# Verify it reads from ibkr_daily.db
sqlite3 ~/SS/Meridian/data/ibkr_daily.db "
SELECT COUNT(*), COUNT(DISTINCT symbol), MAX(date) FROM ibkr_daily_bars
"
```

---

## IMPORTANT CONSTRAINTS

1. **Python 3.14 fix**: Always set asyncio event loop before importing ib_insync
2. **IBKR pacing**: Max 60 hist data requests per 10 min. Batch and pace.
3. **Client IDs**: Vanguard=10, Meridian/S1 daily=11, test scripts=1. No conflicts.
4. **Prefilter = intraday equities ONLY**. Daily bars get full universe. NO prefilter on ibkr_daily.db.
5. **Bar timestamps**: IBKR returns bar OPEN time. Add bar duration to get END time (our convention).
6. **Forex via IDEALPRO**: Use Forex('EURUSD') contract, whatToShow='MIDPOINT'
7. **Crypto via PAXOS**: Use Crypto('BTC', 'PAXOS', 'USD'), only 4 coins (BTC/ETH/LTC/BCH)
8. **Keep Alpaca as fallback**: If IBKR disconnects, fall back to Alpaca for equities
9. **Keep Twelve Data for crypto**: PAXOS only has 4 coins, Twelve Data has 14
10. **S1 + Meridian share ibkr_daily.db**: One DB, both systems read. No S8 cache server needed.
11. **S1 stops using fast_universe_cache.py + yf_cache_pipeline.py for data** — reads from ibkr_daily.db instead
12. **All data subscriptions are FREE** — IDEALPRO FX, PAXOS Crypto, IBKR-PRO equities. No paid bundles needed.

## Commit
```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: IBKR adapter + ibkr_intraday.db + ibkr_daily.db + S1 wiring"
cd ~/SS/Meridian && git add -A && git commit -m "feat: ibkr_daily.db shared daily bars"
cd ~/SS/Advance && git add -A && git commit -m "feat: S1 reads from ibkr_daily.db instead of S8/Alpaca/yfinance"
```
