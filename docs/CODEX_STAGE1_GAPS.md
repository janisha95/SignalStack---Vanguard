# CODEX — Stage 1 Go-Live Gap Fixes
# Based on GPT Universe Builder + Stage 1 Gap Analysis
# Fix the 5 blockers to unify Stage 1 into one multi-source runtime
# Date: Mar 31, 2026

---

## READ FIRST

```bash
# 1. Current V1 entry points (which ones exist?)
ls ~/SS/Vanguard/stages/vanguard_cache.py
ls ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py
ls ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py

# 2. How V7 orchestrator calls V1
grep -n "v1\|cache\|bars\|adapter\|alpaca\|twelve" ~/SS/Vanguard/stages/vanguard_orchestrator.py | head -20

# 3. Universe Builder
find ~/SS/Vanguard -name "*universe_builder*" -o -name "*universe_members*" 2>/dev/null
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".tables" | tr ' ' '\n' | grep universe

# 4. Current bar counts by source
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT data_source, asset_class, COUNT(*) as bars, COUNT(DISTINCT symbol) as symbols
FROM vanguard_bars_1m GROUP BY data_source, asset_class
"

# 5. Twelve Data symbol config
cat ~/SS/Vanguard/config/twelvedata_symbols.json | python3 -c "
import sys,json; d=json.load(sys.stdin)
for k,v in d.items(): print(f'{k}: {len(v)} symbols')
"

# 6. Current V7 cycle — does it poll Twelve Data?
grep -n "twelve\|poll\|forex\|commodity\|crypto" ~/SS/Vanguard/stages/vanguard_orchestrator.py
```

Report ALL output.

---

## FIX 1: Declare one Stage 1 runtime — wire V7 to use both adapters

V7 orchestrator must call BOTH adapters during each cycle:
- Alpaca WebSocket runs continuously (started at session startup)
- Twelve Data polls every cycle (called during V1 step)

File: `~/SS/Vanguard/stages/vanguard_orchestrator.py`

Find the V1/data step in the cycle. Add Twelve Data polling:

```python
# In the startup method:
from vanguard.data_adapters.alpaca_adapter import AlpacaWSAdapter
from vanguard.data_adapters.twelvedata_adapter import TwelveDataAdapter, load_from_config

# Start Alpaca WebSocket for equities (background task)
equity_symbols = get_equity_prefilter()  # 2,000-3,000 symbols
self.alpaca_adapter = AlpacaWSAdapter(equity_symbols)
self.alpaca_task = asyncio.create_task(self.alpaca_adapter.start())

# Initialize Twelve Data adapter for non-equities
self.td_adapter = load_from_config()

# In each cycle's V1 step:
def run_v1(self):
    """Ensure latest bars are in DB from all sources."""
    # Alpaca: running continuously via WebSocket, no action needed per cycle
    # Twelve Data: poll latest bars
    if self.td_adapter:
        bars_written = self.td_adapter.poll_latest_bars()
        logger.info(f"[V1] Twelve Data polled: {bars_written} bars")
    
    # Aggregate 1m → 5m for any new bars
    aggregate_new_bars()
```

If V7 doesn't currently have a V1 step (it may skip straight to V3), add one
as the FIRST step in the cycle.

---

## FIX 2: Create canonical universe_members table

This becomes the single source of truth for "what instruments does Stage 1 cover?"

```sql
CREATE TABLE IF NOT EXISTS vanguard_universe_members (
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,        -- equity, forex, commodity, crypto, index, futures
    data_source TEXT NOT NULL,         -- alpaca, twelvedata, ibkr, mt5
    universe TEXT NOT NULL,            -- ttp_equities, ftmo_forex, ftmo_metals, etc.
    exchange TEXT,                      -- NYSE, NASDAQ, CME, etc.
    tick_value REAL,                    -- for futures
    point_value REAL,                   -- for futures
    session_start TEXT,                 -- "09:30" or null for 24/7
    session_end TEXT,                   -- "16:00" or null for 24/7
    session_tz TEXT DEFAULT 'America/New_York',
    is_active INTEGER DEFAULT 1,
    added_at TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (symbol, data_source)
);
CREATE INDEX IF NOT EXISTS idx_um_asset ON vanguard_universe_members(asset_class);
CREATE INDEX IF NOT EXISTS idx_um_source ON vanguard_universe_members(data_source);
CREATE INDEX IF NOT EXISTS idx_um_universe ON vanguard_universe_members(universe);
```

---

## FIX 3: Populate universe_members from existing sources

Write a function that materializes universe_members from:
- Alpaca assets API → equity rows
- twelvedata_symbols.json → forex/commodity/crypto/index rows
- ftmo_universe_with_futures.json → futures rows (staged, is_active=0)

```python
def materialize_universe():
    """
    Populate vanguard_universe_members from all configured sources.
    Run at V7 startup and at prefilter refresh times.
    """
    con = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()

    # 1. Equity symbols from Alpaca prefilter
    equity_symbols = get_equity_prefilter()
    for sym in equity_symbols:
        con.execute("""
            INSERT OR REPLACE INTO vanguard_universe_members
            (symbol, asset_class, data_source, universe, is_active, last_seen_at)
            VALUES (?, 'equity', 'alpaca', 'ttp_equities', 1, ?)
        """, (sym, now))

    # 2. Non-equity from Twelve Data config
    td_config = load_twelvedata_symbols()
    for asset_class, symbols in td_config.items():
        for sym_entry in symbols:
            sym = sym_entry if isinstance(sym_entry, str) else sym_entry["symbol"]
            universe = f"ftmo_{asset_class}"
            con.execute("""
                INSERT OR REPLACE INTO vanguard_universe_members
                (symbol, asset_class, data_source, universe, is_active, last_seen_at)
                VALUES (?, ?, 'twelvedata', ?, 1, ?)
            """, (sym, asset_class, universe, now))

    # 3. Futures from config (staged, is_active=0)
    futures_config = load_futures_config()
    if futures_config:
        for group in futures_config.get("futures_instruments", {}).values():
            if isinstance(group, dict) and "symbols" in group:
                for sym_entry in group["symbols"]:
                    con.execute("""
                        INSERT OR REPLACE INTO vanguard_universe_members
                        (symbol, asset_class, data_source, universe,
                         tick_value, point_value, exchange, is_active, last_seen_at)
                        VALUES (?, 'futures', 'ibkr', 'topstep_futures',
                                ?, ?, ?, 0, ?)
                    """, (
                        sym_entry["symbol"],
                        parse_tick_value(sym_entry.get("tick_value")),
                        parse_tick_value(sym_entry.get("point_value")),
                        sym_entry.get("exchange"),
                        now,
                    ))

    con.commit()
    con.close()
```

Call this at V7 startup and at each prefilter refresh (9:35, 12:00, 15:00 ET).

---

## FIX 4: Make adapters read from universe_members

Instead of Twelve Data reading from its own JSON config, and Alpaca from assets API,
both should read from universe_members:

```python
# In Twelve Data adapter, get symbols from DB:
def get_active_symbols(data_source="twelvedata"):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT symbol, asset_class FROM vanguard_universe_members
        WHERE data_source = ? AND is_active = 1
    """, (data_source,)).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}

# In Alpaca adapter, get symbols from DB:
def get_active_equity_symbols():
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("""
        SELECT symbol FROM vanguard_universe_members
        WHERE data_source = 'alpaca' AND is_active = 1
    """).fetchall()
    con.close()
    return [r[0] for r in rows]
```

This makes Universe Builder the canonical input bus.

---

## FIX 5: Add source freshness metadata

Add a source health tracking table:

```sql
CREATE TABLE IF NOT EXISTS vanguard_source_health (
    data_source TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    last_bar_ts TEXT,
    last_poll_ts TEXT,
    symbols_active INTEGER,
    symbols_with_recent_bar INTEGER,
    bars_last_5min INTEGER,
    status TEXT DEFAULT 'unknown',    -- live, stale, error, offline
    last_updated TEXT,
    PRIMARY KEY (data_source, asset_class)
);
```

Update this at the end of each V7 cycle:

```python
def update_source_health():
    """Update source freshness after each cycle."""
    con = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    cutoff_5min = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    for source in ["alpaca", "twelvedata"]:
        for asset_class in con.execute("""
            SELECT DISTINCT asset_class FROM vanguard_universe_members
            WHERE data_source = ? AND is_active = 1
        """, (source,)).fetchall():
            ac = asset_class[0]
            stats = con.execute("""
                SELECT COUNT(DISTINCT symbol), MAX(bar_ts_utc)
                FROM vanguard_bars_1m
                WHERE data_source = ? AND asset_class = ?
            """, (source, ac)).fetchone()

            recent = con.execute("""
                SELECT COUNT(DISTINCT symbol)
                FROM vanguard_bars_1m
                WHERE data_source = ? AND asset_class = ? AND bar_ts_utc >= ?
            """, (source, ac, cutoff_5min)).fetchone()

            status = "live" if recent[0] > 0 else "stale"

            con.execute("""
                INSERT OR REPLACE INTO vanguard_source_health
                (data_source, asset_class, last_bar_ts, symbols_active,
                 symbols_with_recent_bar, status, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (source, ac, stats[1], stats[0], recent[0], status, now))

    con.commit()
    con.close()
```

---

## FIX 6: Expose source health via API

Add to unified_api.py:

```python
@app.get("/api/v1/data/health")
async def data_health():
    """Source-aware freshness for Stage 1 data."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM vanguard_source_health ORDER BY data_source, asset_class").fetchall()
    con.close()
    return {"sources": [dict(r) for r in rows]}
```

---

## VERIFY

```bash
# Compile checks
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_orchestrator.py

# Universe members populated
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT universe, data_source, asset_class, COUNT(*) as symbols, SUM(is_active) as active
FROM vanguard_universe_members GROUP BY universe, data_source, asset_class
"

# Source health
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT * FROM vanguard_source_health
"

# V7 single cycle with Twelve Data
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle --dry-run

# API health endpoint
curl -s http://localhost:8090/api/v1/data/health | python3 -m json.tool
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: Stage 1 unification — universe_members canonical bus, Twelve Data in V7 loop, source health tracking"
```
