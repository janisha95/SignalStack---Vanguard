# CLAUDE CODE — V1 Dual-Adapter: Alpaca + Twelve Data
# Expand V1 cache to handle 2,500+ equities via Alpaca + ~106 non-equity symbols via Twelve Data
# Date: Mar 31, 2026
# CONTEXT: Alpaca IEX free WebSocket tested from Canada — 2,500 symbols confirmed working

---

## READ FIRST

```bash
# 1. Current V1 cache code
head -80 ~/SS/Vanguard/stages/vanguard_cache.py

# 2. Current adapters
ls ~/SS/Vanguard/vanguard/adapters/ 2>/dev/null || ls ~/SS/Vanguard/vanguard/data_adapters/ 2>/dev/null
find ~/SS/Vanguard -name "*alpaca*" -o -name "*adapter*" | grep -v __pycache__ | grep -v .pyc

# 3. Current bar tables
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT 'bars_5m' as tbl, COUNT(*) FROM vanguard_bars_5m
UNION ALL SELECT 'bars_1h', COUNT(*) FROM vanguard_bars_1h
UNION ALL SELECT 'bars_1m', COUNT(*) FROM vanguard_bars_1m
"

# 4. Current universe config
cat ~/SS/Vanguard/config/vanguard_universe.json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({k: len(v) if isinstance(v,list) else v for k,v in d.items()}, indent=2))" 2>/dev/null

# 5. Alpaca env vars
python3 -c "
import os
key = os.environ.get('APCA_API_KEY_ID') or os.environ.get('ALPACA_KEY')
print(f'Alpaca key: {\"present\" if key else \"MISSING\"} ({key[:6]}...)' if key else 'Alpaca key: MISSING')
"

# 6. Check if twelvedata package exists
python3 -c "import twelvedata; print('twelvedata installed:', twelvedata.__version__)" 2>/dev/null || echo "twelvedata NOT installed"

# 7. Twelve Data env var
python3 -c "
import os
key = os.environ.get('TWELVE_DATA_API_KEY')
print(f'Twelve Data key: {\"present\" if key else \"MISSING\"}')
"
```

Report ALL output before writing code.

---

## WHAT TO BUILD

Expand V1's data pipeline to handle two providers:

1. **Alpaca (US equities)**: WebSocket streaming 1m bars for 2,500+ symbols. FREE. Already working from Canada.
2. **Twelve Data (forex/indices/metals/crypto/commodities)**: REST polling every 5 min for ~106 symbols. $79/mo Grow plan.

Both providers write to the same `vanguard_bars_1m` table. The existing bar aggregator (1m → 5m → 1h) works on both — it doesn't care about the source.

---

## ARCHITECTURE

```
vanguard_cache.py (V1 entry point)
│
├── AlpacaAdapter (US equities)
│   ├── WebSocket: wss://stream.data.alpaca.markets/v2/iex
│   ├── Subscribe to 2,500+ symbols for minute bars
│   ├── On bar event → write to vanguard_bars_1m
│   └── Reconnect on disconnect with exponential backoff
│
├── TwelveDataAdapter (forex/indices/metals/crypto/commodities)
│   ├── REST: GET /time_series?symbol={sym}&interval=1min&outputsize=1
│   ├── Poll every 5 minutes (aligned to bar boundaries)
│   ├── Batch: up to 120 symbols per request
│   ├── On response → write to vanguard_bars_1m
│   └── Rate limit: 377 API calls/min (Grow plan)
│
├── Bar Aggregator (existing)
│   ├── 1m → 5m (clock-aligned)
│   └── 5m → 1h (clock-aligned)
│
└── Equity Prefilter (existing, needs expansion)
    ├── Fetch 12K+ assets from Alpaca REST
    ├── Prefilter to 2,000-3,000 tradeable symbols
    └── Refresh at 9:35, 12:00, 15:00 ET
```

---

## FILE STRUCTURE

```
~/SS/Vanguard/
├── vanguard/
│   ├── data_adapters/
│   │   ├── __init__.py
│   │   ├── base_adapter.py          # BaseDataAdapter interface
│   │   ├── alpaca_adapter.py        # WebSocket streaming for US equities
│   │   └── twelvedata_adapter.py    # REST polling for forex/commodities/etc
│   └── helpers/
│       ├── equity_prefilter.py      # 12K → 2-3K equity prefilter
│       └── bar_aggregator.py        # 1m → 5m → 1h (existing)
├── config/
│   ├── vanguard_universe.json       # Symbol lists per asset class
│   └── twelvedata_symbols.json      # Twelve Data symbol mapping
```

---

## BASE ADAPTER INTERFACE

```python
# vanguard/data_adapters/base_adapter.py

from abc import ABC, abstractmethod
from datetime import datetime

class BaseDataAdapter(ABC):
    """Interface for data adapters. All adapters write bars to the same DB table."""

    @abstractmethod
    async def start(self):
        """Start streaming/polling. Non-blocking."""
        pass

    @abstractmethod
    async def stop(self):
        """Stop streaming/polling. Clean shutdown."""
        pass

    @abstractmethod
    def get_status(self) -> dict:
        """Return adapter health status."""
        pass
```

---

## ALPACA ADAPTER

```python
# vanguard/data_adapters/alpaca_adapter.py

"""
Alpaca IEX WebSocket adapter for US equities.
Streams 1m bars for 2,500+ symbols. Free plan. Works from Canada.

Key facts:
- IEX feed only (2% of volume, but real-time OHLC prices)
- No limit on minute bar subscriptions (tested 2,500 from Canada)
- One concurrent WebSocket connection per account
- Bars arrive as they complete (end of each minute)
- Bar timestamp is bar CLOSE time (no conversion needed)
"""

import os
import json
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ALPACA_WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
DB_PATH = os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db")
BATCH_SIZE = 500  # symbols per subscription message


class AlpacaAdapter:
    def __init__(self, symbols: list[str], db_path: str = DB_PATH):
        self.symbols = symbols
        self.db_path = db_path
        self.ws = None
        self.running = False
        self.bars_received = 0
        self.last_bar_ts = None
        self.key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY")
        self.secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET")

    async def start(self):
        """Connect and subscribe to minute bars."""
        import websockets

        self.running = True
        while self.running:
            try:
                async with websockets.connect(ALPACA_WS_URL, ping_interval=30) as ws:
                    self.ws = ws
                    logger.info("[alpaca] Connected to IEX WebSocket")

                    # Auth
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key": self.key,
                        "secret": self.secret,
                    }))
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if msg[0].get("msg") != "authenticated":
                        logger.error(f"[alpaca] Auth failed: {msg}")
                        return

                    # Subscribe in batches
                    for i in range(0, len(self.symbols), BATCH_SIZE):
                        batch = self.symbols[i:i + BATCH_SIZE]
                        await ws.send(json.dumps({
                            "action": "subscribe",
                            "bars": batch,
                        }))
                        # Read confirmation
                        confirm = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                        logger.info(f"[alpaca] Subscribed batch {i // BATCH_SIZE + 1}: {len(batch)} symbols")
                        await asyncio.sleep(0.3)

                    logger.info(f"[alpaca] All {len(self.symbols)} symbols subscribed. Listening...")

                    # Listen for bars
                    while self.running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)
                        bars = [item for item in data if item.get("T") == "b"]
                        if bars:
                            self._write_bars(bars)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[alpaca] WebSocket error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def _write_bars(self, bars: list[dict]):
        """Write Alpaca minute bars to DB."""
        con = sqlite3.connect(self.db_path)
        for bar in bars:
            try:
                con.execute("""
                    INSERT OR REPLACE INTO vanguard_bars_1m
                    (symbol, asset_class, ts_utc, open, high, low, close,
                     volume, tick_volume, source, ingest_ts_utc)
                    VALUES (?, 'equity', ?, ?, ?, ?, ?, ?, ?, 'alpaca_iex', ?)
                """, (
                    bar["S"],           # symbol
                    bar["t"],           # timestamp (bar close, UTC ISO)
                    bar["o"],           # open
                    bar["h"],           # high
                    bar["l"],           # low
                    bar["c"],           # close
                    bar["v"],           # volume
                    bar.get("n", 0),    # trade count (as tick_volume proxy)
                    datetime.now(timezone.utc).isoformat(),
                ))
                self.bars_received += 1
                self.last_bar_ts = bar["t"]
            except Exception as e:
                logger.warning(f"[alpaca] Failed to write bar for {bar.get('S')}: {e}")
        con.commit()
        con.close()

    async def stop(self):
        self.running = False
        if self.ws:
            await self.ws.close()

    def get_status(self):
        return {
            "adapter": "alpaca_iex",
            "symbols": len(self.symbols),
            "bars_received": self.bars_received,
            "last_bar": self.last_bar_ts,
            "running": self.running,
        }
```

---

## TWELVE DATA ADAPTER

```python
# vanguard/data_adapters/twelvedata_adapter.py

"""
Twelve Data REST adapter for non-equity instruments.
Polls 1m bars for forex, indices, metals, crypto, commodities.
Grow plan: 377 API calls/min, no daily limit.

Key facts:
- REST polling (no WebSocket on Grow plan)
- Batch: up to 120 symbols per time_series request
- Rate limit: 377 calls/min
- 1 API credit per symbol per request
- Bar timestamp is bar OPEN time — must add 60s for bar END time
- Historical data available (same endpoint, set outputsize or start_date)
"""

import os
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

TWELVE_DATA_BASE = "https://api.twelvedata.com"
DB_PATH = os.path.expanduser("~/SS/Vanguard/data/vanguard_universe.db")


class TwelveDataAdapter:
    def __init__(self, symbols: dict, db_path: str = DB_PATH):
        """
        symbols: dict mapping symbol to asset_class
            e.g. {"EUR/USD": "forex", "XAU/USD": "metal", "BTC/USD": "crypto"}
        """
        self.symbols = symbols
        self.db_path = db_path
        self.api_key = os.environ.get("TWELVE_DATA_API_KEY")
        self.running = False
        self.bars_received = 0
        self.last_poll_ts = None
        self.errors = []

    def poll_latest_bars(self):
        """
        Fetch latest 1m bar for all symbols via batch REST call.
        Call this every 5 minutes from the orchestrator.
        """
        if not self.api_key:
            logger.error("[twelvedata] No API key. Set TWELVE_DATA_API_KEY env var.")
            return 0

        symbol_list = list(self.symbols.keys())
        total_written = 0

        # Twelve Data supports up to 120 symbols per batch request
        batch_size = 120
        for i in range(0, len(symbol_list), batch_size):
            batch = symbol_list[i:i + batch_size]
            sym_str = ",".join(batch)

            try:
                resp = requests.get(
                    f"{TWELVE_DATA_BASE}/time_series",
                    params={
                        "symbol": sym_str,
                        "interval": "1min",
                        "outputsize": 1,  # just the latest bar
                        "apikey": self.api_key,
                        "timezone": "UTC",
                    },
                    timeout=30,
                )

                if resp.status_code != 200:
                    logger.error(f"[twelvedata] HTTP {resp.status_code}: {resp.text[:200]}")
                    self.errors.append(f"HTTP {resp.status_code}")
                    continue

                data = resp.json()

                # Single symbol returns dict directly, multi returns dict of dicts
                if len(batch) == 1:
                    data = {batch[0]: data}

                written = self._write_bars(data)
                total_written += written

                # Rate limit: small delay between batches
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"[twelvedata] Batch error: {e}")
                self.errors.append(str(e))

        self.last_poll_ts = datetime.now(timezone.utc).isoformat()
        self.bars_received += total_written
        logger.info(f"[twelvedata] Polled {len(symbol_list)} symbols, wrote {total_written} bars")
        return total_written

    def fetch_historical(self, symbol: str, start_date: str, end_date: str = None):
        """
        Fetch historical 1m bars for backfill.
        start_date/end_date: "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
        Returns number of bars written.
        """
        params = {
            "symbol": symbol,
            "interval": "1min",
            "start_date": start_date,
            "apikey": self.api_key,
            "timezone": "UTC",
            "outputsize": 5000,  # max per request
        }
        if end_date:
            params["end_date"] = end_date

        try:
            resp = requests.get(f"{TWELVE_DATA_BASE}/time_series", params=params, timeout=60)
            if resp.status_code != 200:
                logger.error(f"[twelvedata] Historical fetch failed: HTTP {resp.status_code}")
                return 0

            data = resp.json()
            if "values" not in data:
                logger.error(f"[twelvedata] No values in response for {symbol}: {data.get('message', 'unknown')}")
                return 0

            return self._write_historical_bars(symbol, data)

        except Exception as e:
            logger.error(f"[twelvedata] Historical error for {symbol}: {e}")
            return 0

    def _write_bars(self, data: dict) -> int:
        """Write latest bars from batch response."""
        con = sqlite3.connect(self.db_path)
        written = 0
        now = datetime.now(timezone.utc).isoformat()

        for sym, sym_data in data.items():
            if isinstance(sym_data, dict) and "values" in sym_data:
                values = sym_data["values"]
                asset_class = self.symbols.get(sym, "unknown")

                for bar in values:
                    try:
                        # Twelve Data timestamp is bar OPEN time — add 60s for END time
                        bar_open_ts = bar["datetime"]
                        # Parse and add 60 seconds
                        dt = datetime.strptime(bar_open_ts, "%Y-%m-%d %H:%M:%S")
                        dt = dt.replace(tzinfo=timezone.utc)
                        bar_end_ts = (dt + timedelta(seconds=60)).isoformat()

                        con.execute("""
                            INSERT OR REPLACE INTO vanguard_bars_1m
                            (symbol, asset_class, ts_utc, open, high, low, close,
                             volume, tick_volume, source, ingest_ts_utc)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'twelvedata', ?)
                        """, (
                            sym,
                            asset_class,
                            bar_end_ts,
                            float(bar["open"]),
                            float(bar["high"]),
                            float(bar["low"]),
                            float(bar["close"]),
                            int(bar.get("volume", 0)) if bar.get("volume") else 0,
                            now,
                        ))
                        written += 1
                    except Exception as e:
                        logger.warning(f"[twelvedata] Failed to write bar for {sym}: {e}")
            elif isinstance(sym_data, dict) and "code" in sym_data:
                logger.warning(f"[twelvedata] Error for {sym}: {sym_data.get('message', 'unknown')}")

        con.commit()
        con.close()
        return written

    def _write_historical_bars(self, symbol: str, data: dict) -> int:
        """Write historical bars from single-symbol response."""
        asset_class = self.symbols.get(symbol, "unknown")
        values = data.get("values", [])
        con = sqlite3.connect(self.db_path)
        written = 0
        now = datetime.now(timezone.utc).isoformat()

        for bar in values:
            try:
                bar_open_ts = bar["datetime"]
                dt = datetime.strptime(bar_open_ts, "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
                bar_end_ts = (dt + timedelta(seconds=60)).isoformat()

                con.execute("""
                    INSERT OR IGNORE INTO vanguard_bars_1m
                    (symbol, asset_class, ts_utc, open, high, low, close,
                     volume, tick_volume, source, ingest_ts_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'twelvedata', ?)
                """, (
                    symbol, asset_class, bar_end_ts,
                    float(bar["open"]), float(bar["high"]),
                    float(bar["low"]), float(bar["close"]),
                    int(bar.get("volume", 0)) if bar.get("volume") else 0,
                    now,
                ))
                written += 1
            except Exception as e:
                logger.warning(f"[twelvedata] Historical bar write failed: {e}")

        con.commit()
        con.close()
        logger.info(f"[twelvedata] Historical backfill {symbol}: {written} bars written")
        return written

    def get_status(self):
        return {
            "adapter": "twelvedata",
            "symbols": len(self.symbols),
            "bars_received": self.bars_received,
            "last_poll": self.last_poll_ts,
            "errors": len(self.errors),
        }
```

---

## TWELVE DATA SYMBOL CONFIG

Create `config/twelvedata_symbols.json`:

```json
{
    "forex": [
        "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "AUD/USD", "NZD/USD", "USD/CAD",
        "EUR/GBP", "EUR/JPY", "GBP/JPY", "EUR/AUD", "EUR/NZD", "EUR/CHF", "EUR/CAD",
        "GBP/AUD", "GBP/NZD", "GBP/CHF", "GBP/CAD",
        "AUD/JPY", "AUD/NZD", "AUD/CHF", "AUD/CAD",
        "NZD/JPY", "NZD/CHF", "NZD/CAD",
        "CHF/JPY", "CAD/JPY", "CAD/CHF"
    ],
    "index": [
        "SPX", "NDX", "DJI", "RUT",
        "FTSE", "DAX", "CAC", "STOXX50E",
        "N225", "HSI", "ASX200"
    ],
    "metal": [
        "XAU/USD", "XAG/USD", "XPT/USD", "XPD/USD",
        "XCU/USD"
    ],
    "energy": [
        "WTI/USD", "BRN/USD", "NG/USD"
    ],
    "crypto": [
        "BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD",
        "ADA/USD", "AVAX/USD", "DOT/USD", "MATIC/USD", "LINK/USD",
        "DOGE/USD", "SHIB/USD", "UNI/USD", "ATOM/USD", "LTC/USD"
    ],
    "agriculture": [
        "ZC/USD", "ZS/USD", "ZW/USD", "KC/USD", "CT/USD", "SB/USD"
    ]
}
```

Verify these symbols exist on Twelve Data before using. Some symbol formats
may differ (e.g. Twelve Data uses "XAU/USD" not "XAUUSD").

---

## EQUITY PREFILTER EXPANSION

The current prefilter produces 247 symbols. Expand to 2,000-3,000:

```python
def prefilter_equities(min_price=2.0, max_price=500.0,
                       min_avg_volume=100_000, min_market_cap=100_000_000):
    """
    Fetch Alpaca assets and filter to tradeable equities.
    Returns list of ~2,000-3,000 symbols.
    """
    import urllib.request, json, os

    url = "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity"
    key = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_KEY")
    secret = os.environ.get("APCA_API_SECRET_KEY") or os.environ.get("ALPACA_SECRET")

    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    })

    with urllib.request.urlopen(req, timeout=30) as resp:
        assets = json.loads(resp.read())

    # Filter
    survivors = []
    for a in assets:
        if not a.get("tradable"):
            continue
        if a.get("exchange") not in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"):
            continue
        sym = a.get("symbol", "")
        if len(sym) > 5 or sym.endswith(".U") or sym.endswith(".W"):
            continue
        # Skip leveraged/inverse ETFs (optional, remove if you want them)
        name = a.get("name", "").upper()
        if any(x in name for x in ["LEVERAGED", "INVERSE", "ULTRA", "3X", "2X", "-3X", "-2X"]):
            continue
        survivors.append(sym)

    return survivors
```

Run at startup + refresh at 9:35, 12:00, 15:00 ET.

---

## INTEGRATION WITH V7 ORCHESTRATOR

The V7 orchestrator calls V1→V6 each cycle. V1's role in each cycle:

```python
# In vanguard_orchestrator.py cycle:
# V1 step: ensure latest bars are in DB

# Alpaca: running continuously via WebSocket (started at session startup)
# Twelve Data: poll every cycle
twelvedata_adapter.poll_latest_bars()

# Then proceed to V2 (prefilter/health check)
```

At session startup:
```python
# Start Alpaca WebSocket in background task
alpaca_task = asyncio.create_task(alpaca_adapter.start())

# Initial Twelve Data poll
twelvedata_adapter.poll_latest_bars()
```

At session shutdown:
```python
await alpaca_adapter.stop()
```

---

## HISTORICAL BACKFILL SCRIPT

```python
# scripts/backfill_twelvedata.py
"""
Backfill historical 1m bars from Twelve Data for non-equity symbols.
Run once to seed the DB, then V7 keeps it current.

Usage:
    python3 scripts/backfill_twelvedata.py                    # Last 5 days
    python3 scripts/backfill_twelvedata.py --days 30          # Last 30 days
    python3 scripts/backfill_twelvedata.py --symbol EUR/USD   # One symbol
"""
```

Rate limit aware: 377 calls/min on Grow. Each historical fetch = 1 call.
106 symbols × 5 days = 106 calls. Well within limits.

For deeper backfill (30+ days), use Databento if needed.

---

## ENV VARS NEEDED

```bash
# Add to ~/.zshrc or boot_signalstack.sh:
export APCA_API_KEY_ID=your_alpaca_key        # Already have this
export APCA_API_SECRET_KEY=your_alpaca_secret  # Already have this
export TWELVE_DATA_API_KEY=your_td_key         # New — from Twelve Data dashboard
```

---

## INSTALL

```bash
pip install twelvedata websockets --break-system-packages
```

---

## VERIFY

```bash
# 1. Test Twelve Data connection
python3 -c "
import os, requests
key = os.environ.get('TWELVE_DATA_API_KEY')
resp = requests.get('https://api.twelvedata.com/time_series', params={
    'symbol': 'EUR/USD', 'interval': '1min', 'outputsize': 3,
    'apikey': key, 'timezone': 'UTC'
})
data = resp.json()
print(f'EUR/USD latest bars: {len(data.get(\"values\", []))}')
for v in data.get('values', []):
    print(f'  {v[\"datetime\"]} O={v[\"open\"]} H={v[\"high\"]} L={v[\"low\"]} C={v[\"close\"]}')
"

# 2. Test Alpaca WebSocket (quick 30-second test)
python3 -c "
import asyncio, json, os, websockets

async def test():
    key = os.environ.get('APCA_API_KEY_ID')
    secret = os.environ.get('APCA_API_SECRET_KEY')
    async with websockets.connect('wss://stream.data.alpaca.markets/v2/iex') as ws:
        await ws.recv()  # connected
        await ws.send(json.dumps({'action':'auth','key':key,'secret':secret}))
        msg = json.loads(await ws.recv())
        print(f'Auth: {msg[0].get(\"msg\")}')
        await ws.send(json.dumps({'action':'subscribe','bars':['AAPL','MSFT','TSLA']}))
        msg = json.loads(await ws.recv())
        print(f'Subscribed: {len(msg[0].get(\"bars\",[]))} symbols')
        print('Listening 30s...')
        import time; start = time.time()
        while time.time() - start < 30:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                bars = [m for m in msg if m.get('T')=='b']
                if bars: print(f'  Bar: {bars[0][\"S\"]} close={bars[0][\"c\"]}')
            except: pass
asyncio.run(test())
"

# 3. Compile check
python3 -m py_compile ~/SS/Vanguard/vanguard/data_adapters/alpaca_adapter.py
python3 -m py_compile ~/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py

# 4. Full test
cd ~/SS/Vanguard && python3 -m pytest tests/test_vanguard_cache.py -v
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: V1 dual-adapter — Alpaca equities + Twelve Data forex/commodities"
```
