# GFT Go-Live Spec — Complete End-to-End

## 1. MT5 on Mac via Docker (FREE — No Windows, No VPS, No Cloud)

GitHub repo: "MetaTrader 5 Solution for macOS Silicon M Series"
Runs MT5 in Docker with Wine on Apple Silicon. Connects via standard Python
`MetaTrader5` package. Completely free.

### Setup:
```bash
# Clone the repo
git clone https://github.com/<repo-name>/mt5-docker-mac-silicon.git
cd mt5-docker-mac-silicon

# Build and run (Docker Desktop must be installed)
docker compose up -d

# Install the Python bridge
pip install MetaTrader5 --break-system-packages

# Test connection
python3 -c "
import MetaTrader5 as mt5
mt5.initialize()
print(f'MT5 version: {mt5.version()}')
print(f'Terminal info: {mt5.terminal_info()}')
mt5.shutdown()
"
```

### Connect to GFT:
1. Open MT5 inside Docker (GUI accessible via VNC or X11 forwarding)
2. Login with GFT credentials:
   - Server: GFT server (from your GFT account)
   - Account: 314799029 (5K) or 314799030 (10K)
   - Password: your GFT password
3. Once connected, the Python `MetaTrader5` package can execute trades

### MT5 Executor Code:

```python
"""
~/SS/Vanguard/vanguard/executors/mt5_executor.py

Execute trades on GFT MT5 via Docker-hosted MetaTrader5.
"""
import MetaTrader5 as mt5
import logging

logger = logging.getLogger("mt5_executor")

# GFT symbol mapping: Vanguard symbol → GFT MT5 symbol
VANGUARD_TO_GFT = {
    # US Shares CFDs (scored by equity model)
    "AAPL": "AAPL.x", "AMZN": "AMZN.x", "BA": "BA.x",
    "COST": "COST.x", "JPM": "JPM.x", "KO": "KO.x",
    "META": "META.x", "MSFT": "MSFT.x", "NFLX": "NFLX.x",
    "NVDA": "NVDA.x", "ORCL": "ORCL.x", "PLTR": "PLTR.x",
    "TSLA": "TSLA.x", "UNH": "UNH.x", "V": "V.x",
    # Index CFDs (scored by equity model via ETF proxy)
    "SPY": "SPX500.x", "QQQ": "NAS100.x", "DIA": "US30.x",
    # Forex (scored by forex model — direct mapping)
    "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD", "NZDUSD": "NZDUSD", "USDCAD": "USDCAD",
    "USDCHF": "USDCHF", "EURGBP": "EURGBP", "EURJPY": "EURJPY",
    "GBPJPY": "GBPJPY", "AUDJPY": "AUDJPY", "EURCHF": "EURCHF",
    "EURAUD": "EURAUD", "EURCAD": "EURCAD",
}

# Reverse mapping for reading positions
GFT_TO_VANGUARD = {v: k for k, v in VANGUARD_TO_GFT.items()}


def connect() -> bool:
    """Initialize MT5 connection to GFT."""
    if not mt5.initialize():
        logger.error(f"MT5 init failed: {mt5.last_error()}")
        return False
    info = mt5.terminal_info()
    logger.info(f"MT5 connected: {info.company} build {info.build}")
    return True


def execute_trade(symbol: str, direction: str, lot_size: float,
                  stop_loss: float = None, take_profit: float = None,
                  comment: str = "") -> dict:
    """
    Place a market order on GFT MT5.
    
    symbol: Vanguard symbol (e.g., "AAPL", "EURUSD")
    direction: "LONG" or "SHORT"
    lot_size: Trade volume (0.01 = micro lot for forex, 1 = 1 share for CFD)
    """
    gft_symbol = VANGUARD_TO_GFT.get(symbol, symbol)
    
    # Get current price
    tick = mt5.symbol_info_tick(gft_symbol)
    if tick is None:
        return {"status": "failed", "error": f"No tick for {gft_symbol}"}
    
    price = tick.ask if direction == "LONG" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": gft_symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "deviation": 20,  # slippage in points
        "magic": 123456,  # EA magic number for tracking
        "comment": comment or f"SS_{symbol}_{direction}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    if stop_loss:
        request["sl"] = stop_loss
    if take_profit:
        request["tp"] = take_profit
    
    result = mt5.order_send(request)
    
    if result is None:
        return {"status": "failed", "error": str(mt5.last_error())}
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"status": "failed", "retcode": result.retcode,
                "error": result.comment}
    
    logger.info(f"[MT5] {direction} {gft_symbol} x{lot_size} @ {price} "
                f"→ ticket={result.order}")
    
    return {
        "status": "filled",
        "ticket": result.order,
        "price": price,
        "volume": lot_size,
        "symbol": gft_symbol,
    }


def close_position(ticket: int) -> dict:
    """Close an open position by ticket."""
    position = None
    for pos in mt5.positions_get():
        if pos.ticket == ticket:
            position = pos
            break
    
    if position is None:
        return {"status": "failed", "error": f"Position {ticket} not found"}
    
    close_type = mt5.ORDER_TYPE_SELL if position.type == 0 else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(position.symbol)
    price = tick.bid if position.type == 0 else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": position.volume,
        "type": close_type,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "magic": 123456,
        "comment": "SS_close",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"status": "closed", "ticket": ticket, "price": price}
    return {"status": "failed", "error": str(result)}


def get_positions() -> list:
    """Get all open positions."""
    positions = mt5.positions_get()
    if positions is None:
        return []
    return [
        {
            "ticket": p.ticket,
            "symbol": p.symbol,
            "vanguard_symbol": GFT_TO_VANGUARD.get(p.symbol, p.symbol),
            "direction": "LONG" if p.type == 0 else "SHORT",
            "volume": p.volume,
            "price_open": p.price_open,
            "price_current": p.price_current,
            "profit": p.profit,
            "sl": p.sl,
            "tp": p.tp,
        }
        for p in positions
    ]


def get_account_info() -> dict:
    """Get account balance, equity, margin."""
    info = mt5.account_info()
    if info is None:
        return {}
    return {
        "balance": info.balance,
        "equity": info.equity,
        "margin": info.margin,
        "free_margin": info.margin_free,
        "profit": info.profit,
    }
```

---

## 2. GFT Universe — What To Scan and Score

### GFT Instruments (from screenshots)

```python
# ~/SS/Vanguard/config/gft_universe.json
{
    "us_shares": {
        "note": "Score with EQUITY model. Map AAPL→AAPL.x for execution.",
        "symbols": ["AAPL", "AMZN", "BA", "COST", "JPM", "KO", "META",
                     "MSFT", "NFLX", "NVDA", "ORCL", "PLTR", "TSLA", "UNH", "V"]
    },
    "index_cfds": {
        "note": "Score with EQUITY model via ETF proxy. Map SPY→SPX500.x etc.",
        "symbols": ["SPY", "QQQ", "DIA"],
        "gft_mapping": {"SPY": "SPX500.x", "QQQ": "NAS100.x", "DIA": "US30.x"}
    },
    "forex": {
        "note": "Score with FOREX model. Direct symbol mapping.",
        "symbols": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD",
                     "USDCAD", "USDCHF", "EURGBP", "EURJPY", "GBPJPY",
                     "AUDJPY", "EURCHF", "EURAUD", "EURCAD"]
    }
}
```

### Key Insight: No New Models Needed

| GFT Instrument | Vanguard Symbol | Model Used | Why It Works |
|---|---|---|---|
| AAPL.x (US Share CFD) | AAPL | **equity** ensemble | Same stock, CFD is just a derivative |
| NAS100.x (Index CFD) | QQQ | **equity** ensemble | QQQ tracks NAS100 identically |
| SPX500.x (Index CFD) | SPY | **equity** ensemble | SPY tracks SPX500 identically |
| EURUSD (Forex) | EURUSD | **forex** ensemble | Direct 1:1 |

The equity model already scores AAPL, AMZN, NVDA, TSLA, QQQ, SPY, DIA.
The forex model already scores all 14 pairs.
No training needed. Just filter V6 output for GFT symbols.

---

## 3. How To Filter GFT Universe in V6

### Option A: V6 instrument_scope filter (RECOMMENDED)

Add `gft_universe` as an instrument_scope in V6 risk filters.

File: `~/SS/Vanguard/stages/vanguard_risk_filters.py`

Find the instrument scope filter (where it checks `us_equities`, `forex_cfd`, etc.):

```bash
grep -n "instrument_scope\|instrument_filter\|us_equities\|forex_cfd" \
  ~/SS/Vanguard/stages/vanguard_risk_filters.py | head -15
```

Add a `gft_universe` scope check:

```python
GFT_UNIVERSE = {
    # US Shares (equity model)
    'AAPL', 'AMZN', 'BA', 'COST', 'JPM', 'KO', 'META', 'MSFT', 'NFLX',
    'NVDA', 'ORCL', 'PLTR', 'TSLA', 'UNH', 'V',
    # Index proxies (equity model)
    'SPY', 'QQQ', 'DIA',
    # Forex (forex model)
    'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'NZDUSD', 'USDCAD',
    'USDCHF', 'EURGBP', 'EURJPY', 'GBPJPY', 'AUDJPY', 'EURCHF',
    'EURAUD', 'EURCAD',
}

# In the instrument scope filter:
if scope == 'gft_universe':
    if symbol not in GFT_UNIVERSE:
        return reject(candidate, f"NOT_IN_GFT_UNIVERSE:{symbol}")
    # GFT accepts both equity CFDs and forex
    pass  # symbol is in GFT universe, proceed
```

### Option B: Add GFT accounts to DB

```sql
-- Run this in sqlite3
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
INSERT OR REPLACE INTO account_profiles (
    id, name, prop_firm, account_type, account_size,
    daily_loss_limit, max_drawdown, max_positions,
    instrument_scope, holding_style, risk_per_trade_pct,
    max_per_sector, max_portfolio_heat_pct,
    max_single_position_pct, max_batch_exposure_pct,
    dll_headroom_pct, dd_headroom_pct,
    max_correlation, max_trades_per_day,
    environment, is_active
) VALUES 
('gft_5k', 'GFT 5K Normal', 'GFT', 'challenge', 5000,
 0.04, 0.08, 5, 'gft_universe', 'intraday', 0.01,
 50, 1.0, 1.0, 1.0, 0.70, 0.80, 1.0, 30, 'live', 1),
('gft_10k', 'GFT 10K Pay Later', 'GFT', 'challenge', 10000,
 0.04, 0.08, 5, 'gft_universe', 'intraday', 0.01,
 50, 1.0, 1.0, 1.0, 0.70, 0.80, 1.0, 30, 'live', 1);
"
```

### Verification After Implementation:

```bash
# Run single cycle
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle 2>&1 | tail -20

# Check GFT approved picks
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "
SELECT account_id, symbol, direction, ROUND(edge_score, 4) as edge
FROM vanguard_tradeable_portfolio
WHERE cycle_ts_utc = (SELECT MAX(cycle_ts_utc) FROM vanguard_tradeable_portfolio)
AND account_id IN ('gft_5k', 'gft_10k')
AND status = 'APPROVED'
ORDER BY account_id, edge_score DESC
"
```

Expected: GFT accounts show ONLY the 15 stocks + forex pairs, scored by
existing equity + forex models, with proper edge_scores.

---

## 4. Wire MT5 Executor into V7

In `vanguard_orchestrator.py`, after V6 approves trades, execute GFT trades:

```python
# After V6, for GFT accounts only:
if execution_mode in ('paper', 'live'):
    gft_trades = [t for t in approved 
                  if t['account_id'] in ('gft_5k', 'gft_10k')]
    
    if gft_trades:
        from vanguard.executors.mt5_executor import connect, execute_trade
        if connect():
            for trade in gft_trades:
                result = execute_trade(
                    symbol=trade['symbol'],
                    direction=trade['direction'],
                    lot_size=trade['shares_or_lots'],
                    stop_loss=trade.get('stop_price'),
                    take_profit=trade.get('tp_price'),
                    comment=f"SS_{trade['symbol']}_{trade['direction']}"
                )
                logger.info(f"[MT5] {trade['symbol']} → {result['status']}")
```

---

## 5. GFT Risk Rules (from GFT website)

| Rule | GFT 5K | GFT 10K |
|---|---|---|
| Daily loss limit | 4% ($200) | 4% ($400) |
| Max drawdown | 8% ($400) | 8% ($800) |
| Max positions | 5 | 5 |
| Profit target (challenge) | 8% ($400) | 8% ($800) |
| Min trading days | 5 | 5 |
| Max lot size (forex) | 5.0 lots | 10.0 lots |
| Max lot size (equity CFD) | Varies | Varies |
| Leverage | Up to 1:100 forex, 1:20 stocks | Same |

---

## 6. Quick Test Sequence

```bash
# 1. Set up MT5 Docker
docker compose up -d

# 2. Test MT5 connection
python3 -c "
import MetaTrader5 as mt5
mt5.initialize()
print(mt5.account_info())
mt5.shutdown()
"

# 3. Add GFT accounts to DB (SQL above)

# 4. Implement GFT_UNIVERSE filter in V6

# 5. Run cycle and verify GFT picks
cd ~/SS/Vanguard && python3 stages/vanguard_orchestrator.py --single-cycle

# 6. Check GFT approved picks (SQL above)

# 7. Place test paper trade
python3 -c "
from vanguard.executors.mt5_executor import connect, execute_trade
connect()
result = execute_trade('EURUSD', 'LONG', 0.01)  # micro lot test
print(result)
"
```
