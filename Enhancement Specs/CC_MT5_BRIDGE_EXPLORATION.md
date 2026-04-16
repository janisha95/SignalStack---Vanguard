# CC Exploration: MT5 Local Bridge via DWX Connect

**Work directory:** `/Users/sjani008/SS/Vanguard/`
**Priority:** Exploration — test if it works, don't wire into production yet
**Time estimate:** 30-60 minutes

---

## Context

MT5 is running locally on Mac via Wine (wine-64 preloader). It connects to the GFT broker and has access to all GFT symbols with live quotes, OHLCV bars, account state, and order execution.

If we can bridge Python ↔ MT5 locally, we eliminate:
- MetaApi cloud subscription ($20-50/mo) for execution
- TwelveData subscription ($35/mo) for forex/crypto data
- IBKR dependency for forex streaming
- Network latency (local vs cloud relay)

## Option A: DWX ZeroMQ Connect (preferred)

### What it is
DWX Connect is an open-source MQL5 Expert Advisor that runs inside MT5 and exposes a ZeroMQ socket server. Native Python connects via `pyzmq` to `localhost:15555` and can:
- Get live quotes (bid/ask) for any symbol
- Request OHLCV bars (any timeframe)
- Place/modify/close orders
- Read account state (balance, equity, margin)
- Read open positions with live P&L

### Step 1: Install pyzmq
```bash
pip install pyzmq --break-system-packages
```

### Step 2: Find the MT5 data directory inside Wine
The MT5 Wine installation has a data directory where EAs go. Find it:
```bash
# Look for the MT5 installation
find ~/.wine -name "terminal64.exe" 2>/dev/null
find ~/Library -name "terminal64.exe" 2>/dev/null
find / -name "MetaTrader 5" -type d 2>/dev/null 2>&1 | head -5

# Also check for the MQL5 directory
find ~/.wine -name "MQL5" -type d 2>/dev/null
find ~/Library -name "MQL5" -type d 2>/dev/null
```

The EA files need to go into: `<MT5_DATA_DIR>/MQL5/Experts/`
The include files go into: `<MT5_DATA_DIR>/MQL5/Include/`

### Step 3: Download DWX Connect
```bash
cd /tmp
git clone https://github.com/darwinex/dwxconnect.git
ls dwxconnect/
```

Look at the repository structure:
- `mql5/` — contains the EA (.mq5) and include files
- `python/` — contains the Python client library

### Step 4: Copy EA files to MT5
```bash
# After finding the MQL5 directory in Step 2:
MT5_MQL5="<path_from_step_2>/MQL5"

# Copy the EA
cp /tmp/dwxconnect/mql5/Experts/DWX_Connect.mq5 "$MT5_MQL5/Experts/"

# Copy include files
cp /tmp/dwxconnect/mql5/Include/DWX/*.mqh "$MT5_MQL5/Include/DWX/" 2>/dev/null
# If DWX dir doesn't exist:
mkdir -p "$MT5_MQL5/Include/DWX"
cp /tmp/dwxconnect/mql5/Include/DWX/*.mqh "$MT5_MQL5/Include/DWX/"
```

**NOTE:** The EA needs to be COMPILED inside MT5. Open MT5 → MetaEditor → Open DWX_Connect.mq5 → Compile. OR the repo may have a pre-compiled .ex5 file.

### Step 5: Enable the EA in MT5
1. In MT5: Tools → Options → Expert Advisors
2. Check "Allow algorithmic trading"
3. Check "Allow DLL imports" (needed for ZeroMQ)
4. Drag DWX_Connect onto any chart
5. The EA should show a smiley face in the top-right of the chart

### Step 6: Test Python connection
```python
#!/usr/bin/env python3
"""Quick test: can we connect to MT5 via DWX?"""
import sys
sys.path.insert(0, '/tmp/dwxconnect/python')

from api.dwx_client import dwx_client

# Default ports: PUSH=15555, PULL=15556, PUB=15557
client = dwx_client(host='localhost', push_port=15555, pull_port=15556, pub_port=15557)

# Test 1: Get account info
print("Account info:", client.account_info)

# Test 2: Get open positions  
print("Open orders:", client.open_orders)

# Test 3: Get live quote
client.subscribe_symbols(['EURUSD'])
import time; time.sleep(2)
print("Market data:", client.market_data)

# Test 4: Get bars
client.get_historic_data('EURUSD', 'M5', num_candles=10)
time.sleep(2)
print("Historic data:", client.historic_data)

client.close()
```

Save as `/tmp/test_mt5_dwx.py` and run:
```bash
python3 /tmp/test_mt5_dwx.py
```

### Step 7: Report results

Report back with:
1. Was pyzmq installable? 
2. Could you find the MT5 MQL5 directory?
3. Could you copy the EA files?
4. Does the Python test connect?
5. Does `account_info` return balance/equity?
6. Does `market_data` return live EURUSD bid/ask?
7. Does `historic_data` return 5m OHLCV bars?

---

## Option B: Direct MetaTrader5 Python Package (fallback)

This is the simpler API but requires Python to run INSIDE Wine.

### Test if the package works natively first (it probably won't):
```bash
pip install MetaTrader5 --break-system-packages
python3 -c "import MetaTrader5; print(MetaTrader5.__file__)"
```

If this fails with a DLL error (expected), try:

### Install Python inside Wine:
```bash
# Check if Wine has Python
wine python3 --version 2>/dev/null
wine python --version 2>/dev/null

# If not, download Python for Windows inside Wine
# This is more complex — only try if Option A fails
```

---

## Option C: Simple File Bridge (simplest fallback)

If ZeroMQ doesn't work through Wine, use file-based IPC:

### Create a simple MQL5 script that writes data to a file:
```mql5
// Save as FileExport.mq5 in Experts/
void OnTimer() {
    int handle = FileOpen("live_quotes.csv", FILE_WRITE|FILE_CSV);
    // Write all GFT symbol quotes
    string symbols[] = {"EURUSD", "GBPUSD", "USDJPY", ...};
    for(int i=0; i<ArraySize(symbols); i++) {
        double bid = SymbolInfoDouble(symbols[i], SYMBOL_BID);
        double ask = SymbolInfoDouble(symbols[i], SYMBOL_ASK);
        FileWrite(handle, symbols[i], bid, ask, TimeLocal());
    }
    FileClose(handle);
}
```

Python reads the CSV from the Wine filesystem:
```python
# The file will be at: <MT5_DATA_DIR>/MQL5/Files/live_quotes.csv
quotes = pd.read_csv(quotes_path)
```

---

## What to try (in order)

1. **First:** `pip install pyzmq` — verify it installs
2. **Second:** Find the MT5 MQL5 directory in the Wine filesystem
3. **Third:** Clone DWX Connect and copy files
4. **Fourth:** Try the Python test script
5. **If Step 4 fails:** Check if ZeroMQ DLLs need to be in the Wine MT5 directory
6. **If ZeroMQ won't work through Wine:** Try Option C (file bridge)

## DO NOT wire anything into the Vanguard pipeline yet. 
This is exploration only. Report what works and what doesn't.
