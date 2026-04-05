# MT5 Executor Spec + GFT Go-Live Checklist

## GFT Universe (from screenshots)

### US Shares CFDs (15 stocks)
AAPL.x, AMZN.x, BA.x, COST.x, JPM.x, KO.x, META.x, MSFT.x, NFLX.x,
NVDA.x, ORCL.x, PLTR.x, TSLA.x, UNH.x, V.x

### Index CFDs (7)
AUS200.x, GER40.x, JAP225.x, NAS100.x, SPX500.x, UK100.x, US30.x

### Forex (already in Vanguard — 14 pairs via IBKR IDEALPRO)
EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD, USDCHF,
EURGBP, EURJPY, GBPJPY, AUDJPY, EURCHF, EURAUD, EURCAD

### Metals/Energy (from FTMO universe)
XAUUSD, XAGUSD, USOIL.cash, NATGAS.cash

---

## Symbol Mapping: GFT MT5 → Vanguard

```python
GFT_TO_VANGUARD = {
    # US Shares CFDs → equity model
    "AAPL.x": "AAPL", "AMZN.x": "AMZN", "BA.x": "BA",
    "COST.x": "COST", "JPM.x": "JPM", "KO.x": "KO",
    "META.x": "META", "MSFT.x": "MSFT", "NFLX.x": "NFLX",
    "NVDA.x": "NVDA", "ORCL.x": "ORCL", "PLTR.x": "PLTR",
    "TSLA.x": "TSLA", "UNH.x": "UNH", "V.x": "V",
    
    # Index CFDs → use equity model (same underlying dynamics)
    "NAS100.x": "QQQ",    # NAS100 ≈ QQQ ETF
    "SPX500.x": "SPY",    # SPX500 ≈ SPY ETF
    "US30.x": "DIA",      # US30 ≈ DIA ETF
    
    # Forex → forex model (direct 1:1 mapping)
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
    # ... all 14 pairs map directly
    
    # Metals → no model yet (IC was -0.05, skip for now)
    # "XAUUSD": "XAUUSD",
}
```

Key insight: GFT's US Shares and Index CFDs use the EQUITY model. The symbol
suffix `.x` is just GFT's naming convention. AAPL.x trades identical to AAPL.

---

## MT5 Execution: Mac Options

The `MetaTrader5` Python package only works on Windows. Three options for Mac:

### Option A: MetaApi.cloud (RECOMMENDED for speed)
- Cloud REST API that connects to any MT5 broker
- Python SDK: `pip install metaapi-cloud-sdk`
- Cost: Free tier (1 account), $13-20/mo (multiple accounts)
- Latency: ~100-200ms (cloud)
- Setup: Create account on metaapi.cloud, connect GFT MT5 credentials
- No Windows needed

### Option B: Windows VPS ($5/mo)
- Cheap VPS (Contabo, Hetzner, DigitalOcean)
- Install MT5 + Python + MetaTrader5 library
- Run executor script on VPS
- Vanguard sends signals via webhook → VPS executes on MT5
- Latency: ~50ms (VPS to broker)

### Option C: Docker + Wine on Mac
- Complex setup, fragile
- NOT recommended for production

---

## MT5 Executor Architecture

```
Vanguard V7 (Mac)
│
├── mode=live for GFT accounts
│
└── For each approved GFT trade:
    │
    ├── Map symbol: AAPL → AAPL.x (GFT naming)
    ├── Calculate lot size from risk rules
    ├── Set SL/TP from ATR
    │
    └── Send to MT5 executor:
        │
        ├── Option A: MetaApi REST call
        │   POST https://mt-client-api-v1.agiliumtrade.agiliumtrade.ai/
        │   {symbol: "AAPL.x", action: "ORDER_TYPE_BUY", volume: 0.1, ...}
        │
        └── Option B: Webhook → Windows VPS → mt5.order_send()
```

---

## MetaApi Implementation (Option A — recommended)

### Setup (one-time):
```bash
pip install metaapi-cloud-sdk --break-system-packages
```

### Executor code:

```python
"""
mt5_executor.py — GFT trade executor via MetaApi.cloud

Usage:
    from vanguard.executors.mt5_executor import MT5Executor
    
    executor = MT5Executor(
        token="YOUR_METAAPI_TOKEN",
        account_id="YOUR_GFT_ACCOUNT_ID"
    )
    
    # Execute a trade
    result = executor.execute_trade(
        symbol="AAPL.x",
        direction="LONG",
        lot_size=0.1,
        stop_loss_pips=50,
        take_profit_pips=100
    )
"""
import asyncio
from metaapi_cloud_sdk import MetaApi

# GFT symbol mapping
VANGUARD_TO_GFT = {
    # US Shares
    "AAPL": "AAPL.x", "AMZN": "AMZN.x", "BA": "BA.x",
    "COST": "COST.x", "JPM": "JPM.x", "KO": "KO.x",
    "META": "META.x", "MSFT": "MSFT.x", "NFLX": "NFLX.x",
    "NVDA": "NVDA.x", "ORCL": "ORCL.x", "PLTR": "PLTR.x",
    "TSLA": "TSLA.x", "UNH": "UNH.x", "V": "V.x",
    # Indices → ETF proxy
    "QQQ": "NAS100.x", "SPY": "SPX500.x", "DIA": "US30.x",
    # Forex (direct)
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD", "NZDUSD": "NZDUSD", "USDCAD": "USDCAD",
    "USDCHF": "USDCHF", "EURGBP": "EURGBP", "EURJPY": "EURJPY",
    "GBPJPY": "GBPJPY", "AUDJPY": "AUDJPY", "EURCHF": "EURCHF",
    "EURAUD": "EURAUD", "EURCAD": "EURCAD",
}

class MT5Executor:
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = account_id
        self.api = None
        self.connection = None
    
    async def connect(self):
        """Connect to GFT MT5 account via MetaApi."""
        self.api = MetaApi(self.token)
        account = await self.api.metatrader_account_api.get_account(self.account_id)
        
        if account.state != 'DEPLOYED':
            await account.deploy()
        await account.wait_connected()
        
        self.connection = account.get_rpc_connection()
        await self.connection.connect()
        await self.connection.wait_synchronized()
        
        info = await self.connection.get_account_information()
        print(f"Connected to GFT: balance={info['balance']}, equity={info['equity']}")
    
    async def execute_trade(self, symbol: str, direction: str,
                           lot_size: float, stop_loss_price: float = None,
                           take_profit_price: float = None) -> dict:
        """
        Execute a trade on GFT MT5.
        
        symbol: Vanguard symbol (e.g., "AAPL", "EURUSD")
        direction: "LONG" or "SHORT"
        lot_size: Trade size (0.01 = micro, 0.1 = mini, 1.0 = standard)
        """
        gft_symbol = VANGUARD_TO_GFT.get(symbol, symbol)
        
        if direction == "LONG":
            action = 'ORDER_TYPE_BUY'
        else:
            action = 'ORDER_TYPE_SELL'
        
        order = {
            'symbol': gft_symbol,
            'actionType': action,
            'volume': lot_size,
            'comment': f'SignalStack_{symbol}_{direction}',
        }
        
        if stop_loss_price:
            order['stopLoss'] = stop_loss_price
        if take_profit_price:
            order['takeProfit'] = take_profit_price
        
        try:
            result = await self.connection.create_market_buy_order(
                gft_symbol, lot_size, stop_loss_price, take_profit_price,
                {'comment': order['comment']}
            ) if direction == "LONG" else await self.connection.create_market_sell_order(
                gft_symbol, lot_size, stop_loss_price, take_profit_price,
                {'comment': order['comment']}
            )
            
            print(f"[MT5] {direction} {gft_symbol} x{lot_size} → {result}")
            return {"status": "filled", "result": result}
        except Exception as e:
            print(f"[MT5] FAILED {gft_symbol}: {e}")
            return {"status": "failed", "error": str(e)}
    
    async def close_position(self, position_id: str):
        """Close an open position."""
        return await self.connection.close_position(position_id)
    
    async def get_positions(self):
        """Get all open positions."""
        return await self.connection.get_positions()
    
    async def get_account_info(self):
        """Get account balance, equity, margin."""
        return await self.connection.get_account_information()


# Wire into V7 orchestrator:
async def execute_gft_trades(approved_trades: list, executor: MT5Executor):
    """
    Called by V7 after V6 approves trades for GFT accounts.
    """
    for trade in approved_trades:
        if trade['account_id'] not in ('gft_5k', 'gft_10k'):
            continue
        
        result = await executor.execute_trade(
            symbol=trade['symbol'],
            direction=trade['direction'],
            lot_size=trade['shares_or_lots'],
            stop_loss_price=trade.get('stop_price'),
            take_profit_price=trade.get('tp_price'),
        )
        
        # Log to execution table
        log_execution(trade, result)
```

---

## GFT Account Profiles (add to DB)

```sql
-- GFT 5K Normal (account 314799029)
INSERT OR REPLACE INTO account_profiles (
    id, name, prop_firm, account_type, account_size,
    daily_loss_limit, max_drawdown, max_positions,
    instrument_scope, holding_style, risk_per_trade_pct,
    environment, is_active
) VALUES (
    'gft_5k', 'GFT 5K Normal', 'GFT', 'challenge', 5000,
    0.04, 0.08, 10,
    'forex_cfd', 'intraday', 0.01,
    'live', 1
);

-- GFT 10K Pay Later (account 314799030)
INSERT OR REPLACE INTO account_profiles (
    id, name, prop_firm, account_type, account_size,
    daily_loss_limit, max_drawdown, max_positions,
    instrument_scope, holding_style, risk_per_trade_pct,
    environment, is_active
) VALUES (
    'gft_10k', 'GFT 10K Pay Later', 'GFT', 'challenge', 10000,
    0.04, 0.08, 10,
    'forex_cfd', 'intraday', 0.01,
    'live', 1
);
```

---

## GFT-Only Equity Scan (15 stocks instead of 291)

To scan ONLY GFT's 15 US stocks for the GFT accounts:

### Option A: Filter in V6 (simplest)
V6 already filters by `instrument_scope`. Add a GFT-specific scope:
```python
GFT_EQUITY_UNIVERSE = {'AAPL','AMZN','BA','COST','JPM','KO','META',
                        'MSFT','NFLX','NVDA','ORCL','PLTR','TSLA','UNH','V'}

# In V6, for GFT accounts:
if account.instrument_scope == 'gft_universe':
    if symbol not in GFT_EQUITY_UNIVERSE and asset_class == 'equity':
        reject("NOT_IN_GFT_UNIVERSE")
```

### Option B: Separate shortlist (cleaner)
After V5, create a GFT-specific shortlist that only includes GFT instruments.
This way Vanguard still scans the full universe (for TTP) but GFT sees only its instruments.

---

## Open Items / Go-Live Checklist

### P0 — Must have before Monday
- [ ] MT5 executor connected to GFT (MetaApi or VPS)
- [ ] GFT account profiles added to DB
- [ ] GFT symbol mapping verified (test with paper order)
- [ ] V6 rules for GFT accounts (daily loss 4%, max drawdown 8%)
- [ ] Forward validation: check if Vanguard forex picks would have been profitable

### P1 — Should have before Monday
- [ ] Vanguard Telegram includes forex picks per GFT account
- [ ] IBKR equity streaming (keepUpToDate) for better data
- [ ] ETF exclusion in Vanguard equity (UVIX, SPDN, PSQ in shortlist = bad)
- [ ] Proper V6 risk rules per account (remove testing relaxations)
- [ ] Run S1 + Meridian evening scan on IBKR daily data

### P2 — Nice to have
- [ ] Shared feature module (permanent skew fix)
- [ ] Metal/energy/index models (IC was negative, need more data)
- [ ] Vanguard UI/frontend
- [ ] Forward tracking database with P&L
- [ ] IBKR equity subscription ($3/mo NASDAQ+NYSE)

---

## Debugging Tips (When Opus Isn't Available)

### If picks stop flowing:
```bash
# Check if orchestrator is running
ps aux | grep vanguard_orchestrator

# Check latest cycle
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT cycle_ts_utc, COUNT(*) FROM vanguard_predictions
GROUP BY cycle_ts_utc ORDER BY cycle_ts_utc DESC LIMIT 5
"

# Check V2 survivors
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT asset_class, status, COUNT(*) FROM vanguard_health
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_health)
GROUP BY asset_class, status
"
```

### If V6 rejects everything:
```bash
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, status, rejection_reason, COUNT(*)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
GROUP BY account_id, status, rejection_reason
ORDER BY COUNT(*) DESC LIMIT 20
"
```

### If IBKR disconnects:
- Check IB Gateway is running (green bars)
- Restart: `pkill -f vanguard_orchestrator && sleep 5 && cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 &`

### If Telegram stops:
```bash
grep "telegram\|error\|exception" ~/SS/logs/vanguard_loop.log | tail -20
```

### Common SQL queries:
```bash
# Latest shortlist (all asset classes)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT symbol, asset_class, direction, ROUND(edge_score,4), rank
FROM vanguard_shortlist_v2
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_shortlist_v2)
ORDER BY asset_class, direction, rank LIMIT 30
"

# V6 approved only
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, symbol, direction, ROUND(edge_score,4)
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
AND status='APPROVED' ORDER BY account_id, edge_score DESC
"

# Account profiles
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT id, name, instrument_scope, max_positions, is_active FROM account_profiles
"

# Stage timings
grep "Stage timings" ~/SS/logs/vanguard_loop.log | tail -5
```

### Key environment variables:
```bash
export ALPACA_KEY=yPKAMXTZ4O2VUZYOKVZXP7JTAHT
export ALPACA_SECRET=7bhb11btdfgisxwh2R2tjh5JK1fqpv2KMkT963WnWZcS
export IB_INTRADAY_SYMBOL_LIMIT=200  # or 0 to disable IBKR equity
```

### Start everything:
```bash
# Start servers
~/SS/servers.sh start

# Start Vanguard loop (background)
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --loop --interval 300 &

# Run Meridian daily (evening)
cd ~/SS/Meridian && python3 stages/v2_orchestrator.py --real-ml

# Run S1 daily (evening)
cd ~/SS/Advance && python3 s1_evening_orchestrator.py
```
