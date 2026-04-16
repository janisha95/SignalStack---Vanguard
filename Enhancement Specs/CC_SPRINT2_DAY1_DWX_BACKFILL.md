# CC Sprint 2 Day 1 — DWX Adapter + Executor + Backfill Scripts

**Work directory:** `/Users/sjani008/SS/Vanguard/`
**Priority:** CRITICAL — this unlocks auto-execution and overnight model training
**Rule:** INVESTIGATE before building. Read existing code FIRST.

---

## TASK 1: Build `mt5_dwx_adapter.py` (V1 Data Source)

### What it does
Replaces TwelveData as the primary data source for GFT forex/crypto. Reads bars from MT5 via the DWX file-based bridge. Also exposes order execution methods for Task 2.

### Prerequisites
- MT5 is running locally via Wine with DWX_Server_MT5 EA attached to a chart
- DWX files path: `/Users/sjani008/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files`
- DWX Python client is at `/tmp/dwxconnect/python/api/dwx_client.py`
- The EA writes JSON files to `MQL5/Files/DWX/` every 25ms
- All GFT symbols use `.x` suffix (EURUSD.x, BTCUSD.x, etc.)

### Step 1: Copy DWX client into project
```bash
mkdir -p /Users/sjani008/SS/Vanguard/vanguard/data_adapters/dwx
cp /tmp/dwxconnect/python/api/dwx_client.py /Users/sjani008/SS/Vanguard/vanguard/data_adapters/dwx/
touch /Users/sjani008/SS/Vanguard/vanguard/data_adapters/dwx/__init__.py
```

### Step 2: Read existing adapters FIRST
Before writing anything, read these files to understand the interface:
```
/Users/sjani008/SS/Vanguard/vanguard/data_adapters/twelvedata_adapter.py
/Users/sjani008/SS/Vanguard/vanguard/data_adapters/ibkr_adapter.py
/Users/sjani008/SS/Vanguard/stages/vanguard_orchestrator.py  (search for _poll_twelvedata, _poll_ibkr)
```

The adapter must implement the same interface as TwelveData:
- `poll(symbol_filter=None)` → fetches bars, writes to DB, returns count
- Returns 1m bars that the orchestrator aggregates to 5m

### Step 3: Create the adapter file

**File:** `vanguard/data_adapters/mt5_dwx_adapter.py`

```python
"""
MT5 DWX Adapter — Local bridge to MetaTrader 5 via DWX Connect.

Replaces TwelveData for GFT forex/crypto data.
Also provides order execution for V7 and lifecycle daemon.

Data flow:
  DWX EA (MT5) → JSON files → dwx_client → this adapter → vanguard_bars_1m DB

Config in vanguard_runtime.json:
  "data_sources": {
    "mt5_local": {
      "enabled": true,
      "dwx_files_path": "<path to MQL5/Files>",
      "symbol_suffix": ".x",
      "primary_for": ["forex", "crypto", "index", "commodity", "equity"]
    }
  }
"""
```

**Key methods to implement:**

```python
class MT5DWXAdapter:
    def __init__(self, dwx_files_path: str, db_path: str, symbol_suffix: str = ".x"):
        """
        Args:
            dwx_files_path: Path to MQL5/Files directory (NOT MQL5/Files/DWX — 
                           the dwx_client adds /DWX/ internally)
            db_path: Path to vanguard_universe.db
            symbol_suffix: ".x" for GFT broker
        """
        # Initialize dwx_client with the files path
        # dwx_client expects the parent of DWX/ directory
        pass

    def connect(self) -> bool:
        """Start the DWX client. Returns True if account_info is available."""
        # client = dwx_client(metatrader_dir_path=dwx_files_path)
        # Wait up to 5s for account_info to populate
        # Return True if account_info has 'balance' key
        pass

    def poll(self, symbol_filter: list[str] | None = None) -> int:
        """
        Poll MT5 for latest bars via DWX historic_data.
        
        For each symbol in symbol_filter:
          1. Strip .x suffix for internal use, add .x for DWX calls
          2. Request last 10 bars of M1 data via client.get_historic_data()
          3. Wait for response (check client.historic_data dict)
          4. Write new bars to vanguard_bars_1m table
          5. Return total bars written
        
        IMPORTANT: DWX historic_data is async. After calling get_historic_data(),
        you must sleep/poll until client.historic_data[key] is populated.
        The key format is "SYMBOL_TIMEFRAME" e.g. "EURUSD.x_M1"
        
        IMPORTANT: Rate limit — don't request all symbols at once.
        Request in batches of 10, with 1s sleep between batches.
        """
        pass

    def subscribe_bars(self, symbols: list[str], timeframe: str = "M1"):
        """
        Subscribe to streaming bar data for continuous updates.
        
        Uses client.subscribe_symbols_bar_data([['EURUSD.x', 'M1'], ...])
        After subscribing, client.bar_data dict auto-updates every bar close.
        
        The orchestrator can then read client.bar_data instead of polling.
        """
        pass

    def get_live_quotes(self, symbols: list[str]) -> dict:
        """
        Get current bid/ask for symbols.
        
        Uses client.subscribe_symbols(['EURUSD.x', ...])
        Returns: {'EURUSD': {'bid': 1.1550, 'ask': 1.1551}, ...}
        
        Strip .x suffix in return values for Vanguard compatibility.
        """
        pass

    def get_account_info(self) -> dict:
        """
        Returns: {'balance': 9918.30, 'equity': 9926.85, 'free_margin': ..., 'leverage': 100}
        """
        pass

    def get_open_positions(self) -> dict:
        """
        Returns current open orders from client.open_orders.
        
        Returns: {
            '90248521': {
                'symbol': 'USDCHF',  # stripped .x suffix
                'lots': 0.22,
                'type': 'buy',
                'open_price': 0.7974,
                'sl': 0.79546,
                'tp': 0.80000,
                'pnl': 8.55,
                'swap': 0.0
            }
        }
        """
        pass

    # === EXECUTION METHODS (for Task 2) ===

    def open_order(self, symbol: str, side: str, lots: float, 
                   price: float = 0, sl: float = 0, tp: float = 0) -> str:
        """
        Place an order via DWX.
        
        Args:
            symbol: 'EURUSD' (without .x — adapter adds it)
            side: 'buy' or 'sell'
            lots: position size
            price: 0 for market order, >0 for limit
            sl: stop loss price (0 = no SL)
            tp: take profit price (0 = no TP)
        
        Returns: ticket number as string, or empty string on failure
        
        Uses: client.open_order(symbol='EURUSD.x', order_type='buy', 
                                lots=0.22, price=0, stop_loss=sl, take_profit=tp)
        
        After calling, poll client.open_orders to find the new ticket.
        """
        pass

    def close_order(self, ticket: str, lots: float = 0) -> bool:
        """
        Close an order by ticket number.
        
        Args:
            ticket: MT5 ticket number (string)
            lots: 0 = close full position, >0 = partial close
        
        Uses: client.close_order(ticket=int(ticket), lots=lots)
        Returns: True if order disappears from client.open_orders within 5s
        """
        pass

    def modify_order(self, ticket: str, sl: float = 0, tp: float = 0) -> bool:
        """
        Modify SL/TP on an existing order.
        
        Uses: client.modify_order(ticket=int(ticket), SL=sl, TP=tp)
        Returns: True if modification confirmed in client.open_orders
        """
        pass

    def _to_dwx_symbol(self, symbol: str) -> str:
        """EURUSD → EURUSD.x"""
        if not symbol.endswith(self.symbol_suffix):
            return symbol + self.symbol_suffix
        return symbol

    def _from_dwx_symbol(self, symbol: str) -> str:
        """EURUSD.x → EURUSD"""
        if symbol.endswith(self.symbol_suffix):
            return symbol[:-len(self.symbol_suffix)]
        return symbol

    def write_bars_to_db(self, symbol: str, bars: list[dict]) -> int:
        """
        Write bars to vanguard_bars_1m table.
        
        Use the SAME schema as TwelveData adapter writes.
        Read twelvedata_adapter.py to see the exact column names and types.
        
        Each bar dict from DWX has: open, high, low, close, tick_volume, time
        Map tick_volume → volume column.
        """
        pass
```

### Step 4: Add config to vanguard_runtime.json

Add to `data_sources` section:
```json
"mt5_local": {
    "enabled": true,
    "dwx_files_path": "/Users/sjani008/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files",
    "symbol_suffix": ".x",
    "primary_for": ["forex", "crypto", "index", "commodity", "equity"],
    "note": "MT5 DWX local bridge — replaces TwelveData and MetaApi"
}
```

Set TwelveData to `"enabled": false` (keep the code, just disable in config).

### Step 5: Wire into orchestrator V1

In `vanguard_orchestrator.py`, find where TwelveData is polled (search for `_poll_twelvedata` or `twelvedata_poll`).

Add MT5 DWX as the FIRST data source to try:
```python
# In _run_v1() or equivalent:
if self._mt5_adapter and self._mt5_adapter.is_connected():
    mt5_bars = self._mt5_adapter.poll(symbol_filter=resolved_symbols)
    timings['mt5_poll'] = elapsed
else:
    # Fallback to TwelveData if MT5 not available
    td_bars = self._td_adapter.poll(symbol_filter=resolved_symbols)
```

### Step 6: Test

```bash
# Test the adapter standalone
python3 -c "
from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
adapter = MT5DWXAdapter(
    dwx_files_path='/Users/sjani008/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files',
    db_path='/Users/sjani008/SS/Vanguard/data/vanguard_universe.db'
)
print('Connected:', adapter.connect())
print('Account:', adapter.get_account_info())
print('Positions:', adapter.get_open_positions())
bars = adapter.poll(symbol_filter=['EURUSD', 'GBPUSD', 'USDJPY'])
print('Bars written:', bars)
"
```

Then run 1 orchestrator cycle:
```bash
cd ~/SS/Vanguard
python3 stages/vanguard_orchestrator.py --once --execution-mode manual
```

**Verify:** V1 log shows `mt5_poll` timing instead of `twelvedata_poll`. Bars written to DB. V2-V6 pipeline runs normally.

---

## TASK 2: Wire DWX Executor into Lifecycle Daemon

### What it does
Gives the lifecycle daemon the ability to CLOSE trades, MODIFY SL/TP, and execute signal-driven exits. Currently the daemon detects "should close" but can't act.

### Step 1: Read existing lifecycle daemon
```
/Users/sjani008/SS/Vanguard/vanguard/execution/lifecycle_daemon.py
```

Understand:
- How it reads open positions
- How it detects max_holding_time expiry
- Where it would call close/modify (currently a no-op or MetaApi call)

### Step 2: Replace MetaApi with DWX

Find all MetaApi calls in lifecycle_daemon.py and replace with DWX adapter calls:

```python
# OLD (MetaApi):
# await metaapi.close_position(position_id)

# NEW (DWX):
from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter

class LifecycleDaemon:
    def __init__(self, ...):
        # Initialize DWX adapter
        mt5_config = get_runtime_config().get("data_sources", {}).get("mt5_local", {})
        if mt5_config.get("enabled"):
            self._mt5 = MT5DWXAdapter(
                dwx_files_path=mt5_config["dwx_files_path"],
                db_path=db_path
            )
            self._mt5.connect()
        else:
            self._mt5 = None
```

### Step 3: Add signal-driven exit rules

Add these exit rules to the daemon's main loop. All parameters from JSON config:

```json
// Add to vanguard_runtime.json under a new "exit_rules" section:
"exit_rules": {
    "enabled": true,
    "close_on_signal_flip": true,
    "move_sl_to_breakeven_on_streak_drop": true,
    "min_streak_for_hold": 3,
    "min_hold_minutes": 120,
    "max_hold_minutes": 240,
    "check_interval_seconds": 30
}
```

**Implementation in the daemon loop:**

```python
def check_exit_rules(self, position: dict, current_signal: dict) -> str | None:
    """
    Check if a position should be exited based on signal-driven rules.
    
    Args:
        position: from DWX get_open_positions() — has symbol, type (buy/sell), 
                  open_time, pnl, sl, tp, ticket
        current_signal: from latest V5 shortlist — has direction, streak, edge_score
    
    Returns:
        None = keep position
        'CLOSE_SIGNAL_FLIP' = model flipped direction
        'CLOSE_MAX_HOLD' = exceeded max holding time
        'MODIFY_SL_BREAKEVEN' = streak dropped, move SL to entry
    """
    rules = get_runtime_config().get("exit_rules", {})
    if not rules.get("enabled", False):
        return None
    
    hold_minutes = (now - position['open_time']).total_seconds() / 60
    
    # Rule 1: Don't exit before minimum hold time (unless SL hit by broker)
    if hold_minutes < rules.get("min_hold_minutes", 120):
        return None
    
    # Rule 2: Close on signal flip
    if rules.get("close_on_signal_flip", False):
        pos_direction = 'LONG' if position['type'] == 'buy' else 'SHORT'
        signal_direction = current_signal.get('direction', pos_direction)
        if signal_direction != pos_direction:
            return 'CLOSE_SIGNAL_FLIP'
    
    # Rule 3: Move SL to breakeven when streak drops
    if rules.get("move_sl_to_breakeven_on_streak_drop", False):
        current_streak = current_signal.get('streak', 5)
        min_streak = rules.get("min_streak_for_hold", 3)
        if current_streak < min_streak and position['pnl'] > 0:
            return 'MODIFY_SL_BREAKEVEN'
    
    # Rule 4: Close on max hold time
    if hold_minutes > rules.get("max_hold_minutes", 240):
        return 'CLOSE_MAX_HOLD'
    
    return None
```

**Acting on the exit decision:**

```python
def execute_exit(self, position: dict, reason: str):
    """Execute the exit decision via DWX."""
    ticket = position['ticket']
    
    if reason in ('CLOSE_SIGNAL_FLIP', 'CLOSE_MAX_HOLD'):
        success = self._mt5.close_order(ticket)
        log.info(f"[EXIT] {reason} ticket={ticket} symbol={position['symbol']} "
                 f"pnl={position['pnl']} success={success}")
        # Update trade journal
        self._update_journal(ticket, reason, position['pnl'])
        # Send Telegram alert
        self._send_exit_telegram(position, reason)
    
    elif reason == 'MODIFY_SL_BREAKEVEN':
        new_sl = position['open_price']
        success = self._mt5.modify_order(ticket, sl=new_sl)
        log.info(f"[SL→BE] ticket={ticket} symbol={position['symbol']} "
                 f"new_sl={new_sl} success={success}")
```

### Step 4: Reading current signals for exit decisions

The daemon needs to read the LATEST V5 shortlist for each open position's symbol:

```python
def get_current_signal(self, symbol: str) -> dict:
    """
    Read the latest V5 shortlist entry for this symbol.
    
    Query vanguard_shortlist table:
    SELECT direction, edge_score, streak 
    FROM vanguard_shortlist 
    WHERE symbol = ? 
    ORDER BY cycle_ts DESC LIMIT 1
    """
    pass
```

### Step 5: Test

```bash
# Test DWX execution standalone
python3 -c "
from vanguard.data_adapters.mt5_dwx_adapter import MT5DWXAdapter
adapter = MT5DWXAdapter(...)
adapter.connect()

# Check current positions
positions = adapter.get_open_positions()
print('Open positions:', positions)

# Test modifying SL on existing USDCHF position
if positions:
    ticket = list(positions.keys())[0]
    print(f'Modifying SL on ticket {ticket}')
    # DON'T actually modify in test — just verify the method works
    # adapter.modify_order(ticket, sl=0.79800)
"
```

**Verify:** Lifecycle daemon starts, reads positions from DWX, checks exit rules each cycle, logs decisions. When a signal flip occurs, the daemon closes the position via DWX and updates the journal.

---

## TASK 3: Backfill Scripts (Run Overnight ~10-15 hours)

### What these do
Generate training data for Sprint 2 MTF models. Must run BEFORE model training on Vast.ai.

### Script 1: `scripts/backfill_mtf_bars.py`

Aggregates existing 5m bars into 10m, 15m, 30m bars.

```python
"""
Backfill MTF bars from existing 5m data.

Reads: vanguard_bars_5m table
Writes: vanguard_bars_10m, vanguard_bars_15m, vanguard_bars_30m tables

For each symbol in the universe:
  1. Read all 5m bars from DB
  2. Resample to 10m, 15m, 30m using pandas resample
  3. OHLCV aggregation: open=first, high=max, low=min, close=last, volume=sum
  4. Respect session boundaries:
     - Crypto: 24/7, no gaps
     - Forex: skip Fri 5PM ET → Sun 5PM ET
     - Equity: only 9:30-16:00 ET (no bar spans close)
  5. Write to respective tables

Estimated runtime: 2-4 hours (depends on bar count)
"""

import pandas as pd
import sqlite3
from pathlib import Path

DB_PATH = Path("/Users/sjani008/SS/Vanguard/data/vanguard_universe.db")

def aggregate_bars(df_5m: pd.DataFrame, target_tf_minutes: int) -> pd.DataFrame:
    """
    Resample 5m bars to target timeframe.
    
    Args:
        df_5m: DataFrame with columns [timestamp, open, high, low, close, volume]
                timestamp must be a datetime index
        target_tf_minutes: 10, 15, or 30
    
    Returns:
        DataFrame with same columns, resampled
    """
    rule = f"{target_tf_minutes}min"  # Changed from 'T' to 'min' for newer pandas
    resampled = df_5m.resample(rule).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna(subset=['open'])  # Drop empty periods
    return resampled

def main():
    conn = sqlite3.connect(str(DB_PATH))
    
    # Get all symbols from vanguard_bars_5m
    symbols = pd.read_sql(
        "SELECT DISTINCT symbol FROM vanguard_bars_5m", conn
    )['symbol'].tolist()
    
    print(f"Processing {len(symbols)} symbols")
    
    for tf_minutes in [10, 15, 30]:
        table_name = f"vanguard_bars_{tf_minutes}m"
        
        # Create table if not exists (same schema as vanguard_bars_5m)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                symbol TEXT,
                timestamp TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (symbol, timestamp)
            )
        """)
        
        for i, symbol in enumerate(symbols):
            df = pd.read_sql(
                f"SELECT * FROM vanguard_bars_5m WHERE symbol = ? ORDER BY timestamp",
                conn, params=[symbol], parse_dates=['timestamp'], index_col='timestamp'
            )
            
            if len(df) < 2:
                continue
            
            agg = aggregate_bars(df[['open', 'high', 'low', 'close', 'volume']], tf_minutes)
            agg['symbol'] = symbol
            agg.to_sql(table_name, conn, if_exists='append', method='multi')
            
            if (i + 1) % 10 == 0:
                print(f"  [{tf_minutes}m] {i+1}/{len(symbols)} symbols done")
                conn.commit()
        
        conn.commit()
        print(f"Completed {table_name}: {len(symbols)} symbols")
    
    conn.close()

if __name__ == "__main__":
    main()
```

### Script 2: `scripts/backfill_volume_profile.py`

Computes Volume Profile features from existing 5m bars.

```python
"""
Backfill Volume Profile features from 5m bars.

For each symbol, for each 5m bar:
  - Compute session Volume Profile (rolling window of last 48 bars = 4 hours)
  - Calculate: POC, VAH, VAL, skew
  - Store as additional feature columns

Reads: vanguard_bars_5m
Writes: vanguard_features_vp table (symbol, timestamp, poc, vah, val, 
        poc_distance, vah_distance, val_distance, vp_skew, cum_delta)

Estimated runtime: 4-6 hours
"""

import numpy as np
import pandas as pd
import sqlite3

DB_PATH = "/Users/sjani008/SS/Vanguard/data/vanguard_universe.db"
VP_WINDOW = 48  # bars (4 hours of 5m bars)
VALUE_AREA_PCT = 0.70  # 70% of volume defines the value area

def compute_volume_profile(bars: pd.DataFrame) -> dict:
    """
    Compute Volume Profile from a window of bars.
    
    Args:
        bars: DataFrame with open, high, low, close, volume columns
    
    Returns:
        {
            'poc': float,      # Price level with highest volume
            'vah': float,      # Value Area High
            'val': float,      # Value Area Low  
            'vp_skew': float   # (VAH - POC) / (POC - VAL) — >1 means bullish skew
        }
    """
    if len(bars) < 10:
        return {'poc': np.nan, 'vah': np.nan, 'val': np.nan, 'vp_skew': np.nan}
    
    # Create price bins from the range of bars
    price_min = bars['low'].min()
    price_max = bars['high'].max()
    num_bins = 50  # resolution
    bins = np.linspace(price_min, price_max, num_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    
    # Distribute each bar's volume across price bins it spans
    vol_profile = np.zeros(num_bins)
    for _, bar in bars.iterrows():
        bar_vol = bar['volume'] if bar['volume'] > 0 else 1
        mask = (bin_centers >= bar['low']) & (bin_centers <= bar['high'])
        covered = mask.sum()
        if covered > 0:
            vol_profile[mask] += bar_vol / covered
    
    # POC = price bin with max volume
    poc_idx = np.argmax(vol_profile)
    poc = bin_centers[poc_idx]
    
    # Value Area = smallest range containing VALUE_AREA_PCT of total volume
    total_vol = vol_profile.sum()
    target_vol = total_vol * VALUE_AREA_PCT
    
    # Expand outward from POC
    lo_idx, hi_idx = poc_idx, poc_idx
    current_vol = vol_profile[poc_idx]
    while current_vol < target_vol and (lo_idx > 0 or hi_idx < num_bins - 1):
        expand_lo = vol_profile[lo_idx - 1] if lo_idx > 0 else 0
        expand_hi = vol_profile[hi_idx + 1] if hi_idx < num_bins - 1 else 0
        if expand_lo >= expand_hi and lo_idx > 0:
            lo_idx -= 1
            current_vol += expand_lo
        elif hi_idx < num_bins - 1:
            hi_idx += 1
            current_vol += expand_hi
        else:
            lo_idx -= 1
            current_vol += expand_lo
    
    val = bin_centers[lo_idx]
    vah = bin_centers[hi_idx]
    
    # Skew
    denom = poc - val if poc - val != 0 else 0.0001
    vp_skew = (vah - poc) / denom
    
    return {'poc': poc, 'vah': vah, 'val': val, 'vp_skew': vp_skew}


def compute_volume_delta(bars: pd.DataFrame) -> pd.Series:
    """
    Approximate buy/sell volume from bar data.
    
    Heuristic: if close > open, volume is "buy". If close < open, "sell".
    Delta = buy_volume - sell_volume per bar.
    Cumulative delta = running sum of delta.
    
    This is an APPROXIMATION. Real delta needs tick data.
    """
    delta = bars['volume'].copy()
    delta[bars['close'] < bars['open']] *= -1
    delta[bars['close'] == bars['open']] *= 0
    return delta


def main():
    conn = sqlite3.connect(DB_PATH)
    
    # Create output table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vanguard_features_vp (
            symbol TEXT,
            timestamp TEXT,
            poc REAL,
            vah REAL,
            val REAL,
            poc_distance REAL,
            vah_distance REAL,
            val_distance REAL,
            vp_skew REAL,
            volume_delta REAL,
            cum_delta REAL,
            delta_divergence REAL,
            PRIMARY KEY (symbol, timestamp)
        )
    """)
    
    symbols = pd.read_sql(
        "SELECT DISTINCT symbol FROM vanguard_bars_5m", conn
    )['symbol'].tolist()
    
    print(f"Processing {len(symbols)} symbols for Volume Profile")
    
    for i, symbol in enumerate(symbols):
        df = pd.read_sql(
            "SELECT * FROM vanguard_bars_5m WHERE symbol = ? ORDER BY timestamp",
            conn, params=[symbol], parse_dates=['timestamp']
        )
        
        if len(df) < VP_WINDOW + 10:
            print(f"  Skipping {symbol}: only {len(df)} bars")
            continue
        
        results = []
        deltas = compute_volume_delta(df)
        cum_delta = deltas.cumsum()
        
        for j in range(VP_WINDOW, len(df)):
            window = df.iloc[j - VP_WINDOW:j]
            vp = compute_volume_profile(window)
            
            current_close = df.iloc[j]['close']
            row = {
                'symbol': symbol,
                'timestamp': str(df.iloc[j]['timestamp']),
                'poc': vp['poc'],
                'vah': vp['vah'],
                'val': vp['val'],
                'poc_distance': (current_close - vp['poc']) / vp['poc'] if vp['poc'] else 0,
                'vah_distance': (current_close - vp['vah']) / vp['vah'] if vp['vah'] else 0,
                'val_distance': (current_close - vp['val']) / vp['val'] if vp['val'] else 0,
                'vp_skew': vp['vp_skew'],
                'volume_delta': deltas.iloc[j],
                'cum_delta': cum_delta.iloc[j],
                'delta_divergence': 0  # computed below
            }
            
            # Delta divergence: price direction vs delta direction over last 12 bars
            if j >= VP_WINDOW + 12:
                price_change = df.iloc[j]['close'] - df.iloc[j-12]['close']
                delta_change = cum_delta.iloc[j] - cum_delta.iloc[j-12]
                # Divergence = they disagree on direction (price up, delta down or vice versa)
                row['delta_divergence'] = -1 if (price_change * delta_change < 0) else 1
            
            results.append(row)
        
        if results:
            pd.DataFrame(results).to_sql(
                'vanguard_features_vp', conn, if_exists='append', index=False,
                method='multi'
            )
        
        if (i + 1) % 5 == 0:
            print(f"  VP: {i+1}/{len(symbols)} symbols done ({len(results)} rows for {symbol})")
            conn.commit()
    
    conn.commit()
    conn.close()
    print("Volume Profile backfill complete")

if __name__ == "__main__":
    main()
```

### Script 3: `scripts/backfill_training_data_mtf.py`

Creates the training dataset for MTF model training on Vast.ai.

```python
"""
Backfill training data for multi-timeframe models.

For each (asset_class, timeframe):
  1. Read bars at that TF
  2. Read features at that TF (from V3 factor engine or VP features)
  3. Compute forward returns at the TF-appropriate horizon:
     - 5m: 12 bars forward (1 hour)
     - 10m: 6 bars forward (1 hour)
     - 15m: 4 bars forward (1 hour)
     - 30m: 4 bars forward (2 hours)
  4. Compute TBM label (Triple Barrier Method):
     - profit_mult = 2.0 (TP at 2× ATR)
     - stop_mult = 1.0 (SL at 1× ATR)
     - Label: 1 if TP hit first, 0 if SL hit first, 0.5 if neither
  5. Write to vanguard_training_data_mtf table

CRITICAL: No forward leakage. Forward return uses STRICTLY future bars.

Reads: vanguard_bars_5m, vanguard_bars_10m, vanguard_bars_15m, vanguard_bars_30m,
       vanguard_features_vp
Writes: vanguard_training_data_mtf

Estimated runtime: 4-6 hours

Output table schema:
  asset_class TEXT,
  timeframe TEXT,        -- '5m', '10m', '15m', '30m'
  symbol TEXT,
  asof_ts TEXT,          -- timestamp of the feature row
  forward_return REAL,   -- actual return N bars forward
  tbm_label REAL,        -- 0, 0.5, or 1
  direction TEXT,        -- 'LONG' or 'SHORT' (from tbm_label)
  -- Plus ALL feature columns (44 original + 8 VP features)
"""

# Read vanguard/features/feature_computer.py FIRST to see what features exist
# Then replicate them at each timeframe

# For 5m: features come from existing vanguard_features table
# For 10m/15m/30m: recompute features using bars at that TF

# Forward return formula:
# forward_return = (close[t + horizon] - close[t]) / close[t]

# TBM label formula:
# For each bar, look forward up to horizon bars:
#   - If price hits entry + profit_mult * ATR → label = 1 (winner)
#   - If price hits entry - stop_mult * ATR → label = 0 (loser)
#   - If neither → label = 0.5 (neutral)
#   - For SHORT: reverse the TP/SL logic

# The asset_class is determined from the symbol:
#   classify_asset_class() from vanguard/helpers/universe_builder.py

HORIZONS = {
    '5m': 12,   # 12 bars = 1 hour
    '10m': 6,   # 6 bars = 1 hour
    '15m': 4,   # 4 bars = 1 hour
    '30m': 4,   # 4 bars = 2 hours
}
```

### Running the backfill scripts

**Tonight, after CC finishes writing them:**

```bash
cd ~/SS/Vanguard

# Run all three in parallel (they write to different tables)
nohup python3 scripts/backfill_mtf_bars.py > /tmp/backfill_mtf_bars.log 2>&1 &
echo "MTF bars PID: $!"

# Wait for MTF bars to finish (VP needs 10m/15m/30m bars), then:
nohup python3 scripts/backfill_volume_profile.py > /tmp/backfill_vp.log 2>&1 &
echo "VP PID: $!"

# Training data depends on both MTF bars AND VP features:
# Run AFTER both above complete
nohup python3 scripts/backfill_training_data_mtf.py > /tmp/backfill_training.log 2>&1 &
echo "Training data PID: $!"
```

**Actually, the dependency order is:**
1. `backfill_mtf_bars.py` FIRST (2-4 hours) — creates 10m/15m/30m bars
2. `backfill_volume_profile.py` SECOND (4-6 hours) — needs 5m bars (already exist)
3. `backfill_training_data_mtf.py` THIRD (4-6 hours) — needs bars + VP features

Scripts 1 and 2 can run in PARALLEL (they read different tables).
Script 3 must wait for both 1 and 2 to complete.

**To chain them:**
```bash
nohup bash -c '
  echo "Starting MTF bar aggregation..."
  python3 scripts/backfill_mtf_bars.py
  echo "MTF bars done. Starting VP backfill..."
  python3 scripts/backfill_volume_profile.py  
  echo "VP done. Starting training data..."
  python3 scripts/backfill_training_data_mtf.py
  echo "ALL BACKFILL COMPLETE"
' > /tmp/backfill_all.log 2>&1 &
echo "Backfill chain PID: $!"
```

Or run 1 and 2 in parallel, then 3:
```bash
nohup bash -c '
  python3 scripts/backfill_mtf_bars.py &
  python3 scripts/backfill_volume_profile.py &
  wait
  echo "Bars + VP done. Starting training data..."
  python3 scripts/backfill_training_data_mtf.py
  echo "ALL BACKFILL COMPLETE"
' > /tmp/backfill_all.log 2>&1 &
```

### Verification (next morning)

```bash
# Check row counts
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
  SELECT '5m bars' as tbl, count(*) FROM vanguard_bars_5m
  UNION ALL
  SELECT '10m bars', count(*) FROM vanguard_bars_10m
  UNION ALL
  SELECT '15m bars', count(*) FROM vanguard_bars_15m
  UNION ALL
  SELECT '30m bars', count(*) FROM vanguard_bars_30m
  UNION ALL
  SELECT 'VP features', count(*) FROM vanguard_features_vp
  UNION ALL
  SELECT 'Training MTF', count(*) FROM vanguard_training_data_mtf;
"

# Expected ratios:
# 10m bars ≈ 50% of 5m bars
# 15m bars ≈ 33% of 5m bars
# 30m bars ≈ 17% of 5m bars
# VP features ≈ 90% of 5m bars (minus first VP_WINDOW per symbol)
# Training MTF ≈ 4× VP features (one row per TF per bar)
```

---

## Summary

| Task | Deliverable | Depends on | Test |
|---|---|---|---|
| 1 | `mt5_dwx_adapter.py` + config | MT5 running with DWX EA | 1 orchestrator cycle with mt5_local |
| 2 | Lifecycle daemon with DWX executor + exit rules | Task 1 | Daemon reads positions, logs exit decisions |
| 3 | 3 backfill scripts | Existing 5m bar data | Row counts in DB tables |

**Order of execution:**
1. Task 1 first (DWX adapter) — everything depends on this
2. Task 2 second (lifecycle executor) — needs Task 1's adapter
3. Task 3 third (backfill scripts) — can start as soon as scripts are written, runs overnight
