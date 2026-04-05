# STAGE V7: Vanguard Orchestrator + Execution Bridge — vanguard_orchestrator.py

**Status:** APPROVED
**Project:** Vanguard
**Date:** Mar 29, 2026
**Depends on:** Stages V1-V6

---

## What Stage V7 Does

Two jobs:

1. **Orchestrator** — runs the Vanguard pipeline on a loop during market
   hours. Every 5 minutes: V1→V2→V3→V4B→V5→V6. Manages timing, errors,
   session lifecycle, and produces the final tradeable portfolio.

2. **Execution Bridge** — takes V6 output and places trades via
   SignalStack (TTP) or MT5 API (FTMO). Togglable per account.
   Default: OFF (forward-track only). Includes Telegram alerts.

---

## Runtime Flow

```
09:25 ET  Warm start: bootstrap today's bars, initialize portfolio state
09:30 ET  Market opens (no action — warm-up bars accumulating)
09:35 ET  First full cycle

Every 5 minutes (09:35, 09:40, 09:45 ... 15:25):
  V1  Ingest latest completed 5m bars
  V2  Refresh health / tradable universe
  V3  Compute 35 intraday features
  V4B Score survivors (LGBM, TCN when available)
  V5  Regime gate → Edge score → Rank top 15+15
  V6  Risk filters per account → Sized portfolio
  V7  Execution bridge (if enabled) → Place orders
      Telegram alert → Dashboard update

15:30 ET  No new positions (V6 enforces)
15:50 ET  EOD flatten all day-trade accounts
16:00 ET  Session close → Write daily summary
16:05 ET  Forward tracking snapshot
```

---

## Execution Bridge Architecture

```
V6 Tradeable Portfolio
        │
        ├── execution_enabled: false (default)
        │   └── Write to DB + Telegram alert + Dashboard
        │       (forward tracking mode)
        │
        └── execution_enabled: true
            ├── TTP accounts → SignalStack webhook
            ├── FTMO accounts → MT5 API (MetaTrader5 Python)
            └── Write to DB + Telegram alert + Dashboard
```

### Execution Modes (per account)
| Mode | Behavior |
|---|---|
| `off` | No execution. Picks written to DB, Telegram, dashboard only. |
| `paper` | Send to paper/demo account via bridge. Track fills. |
| `live` | Send to live funded account. Full risk checks enforced. |

---

## SignalStack Integration (TTP)

### How It Works
SignalStack provides a webhook URL per connected broker account.
Vanguard sends a JSON POST to this URL, and SignalStack routes it
to TTP's Trader Evolution platform with sub-second execution.

### Webhook Payload Format (Equities)

**Open Long:**
```json
{
    "symbol": "AAPL",
    "action": "buy",
    "quantity": 100
}
```

**Open Short:**
```json
{
    "symbol": "AAPL",
    "action": "sell_short",
    "quantity": 100
}
```

**Close Long:**
```json
{
    "symbol": "AAPL",
    "action": "sell",
    "quantity": 100
}
```

**Close Short:**
```json
{
    "symbol": "AAPL",
    "action": "buy_to_cover",
    "quantity": 100
}
```

**With Limit Price:**
```json
{
    "symbol": "AAPL",
    "action": "buy",
    "quantity": 100,
    "limit_price": 185.50
}
```

**With Stop Price:**
```json
{
    "symbol": "AAPL",
    "action": "buy",
    "quantity": 100,
    "stop_price": 184.00
}
```

**Stop-Limit:**
```json
{
    "symbol": "AAPL",
    "action": "buy",
    "quantity": 100,
    "limit_price": 185.50,
    "stop_price": 184.00
}
```

**Close All (EOD Flatten):**
```json
{
    "symbol": "AAPL",
    "action": "close",
    "quantity": 100
}
```

### SignalStack Actions Reference
| Action | Use |
|---|---|
| `buy` | Open long |
| `sell` | Close long |
| `sell_short` | Open short |
| `buy_to_cover` | Close short |
| `close` | Close any open position (direction-agnostic) |

### Optional Fields
| Field | Description |
|---|---|
| `quantity_type` | `fixed` (default), `cash`, `percent_of_equity` |
| `limit_price` | Execute at limit price or better |
| `stop_price` | Trigger stop order at this price |

### SignalStack Config
```json
{
    "signalstack": {
        "webhook_url": "ENV:SIGNALSTACK_WEBHOOK_URL",
        "timeout_seconds": 5,
        "max_retries": 2,
        "retry_delay_seconds": 1,
        "order_type": "market",
        "log_payloads": true
    }
}
```

---

## MT5 Integration (FTMO)

### How It Works
MetaTrader5 Python package connects directly to FTMO's MT5 terminal.
No webhook middleman needed — direct API.

### Order Placement
```python
import MetaTrader5 as mt5

def place_mt5_order(symbol, direction, lots, sl_price, tp_price):
    mt5.initialize()

    order_type = mt5.ORDER_TYPE_BUY if direction == 'LONG' else mt5.ORDER_TYPE_SELL
    price = mt5.symbol_info_tick(symbol).ask if direction == 'LONG' else mt5.symbol_info_tick(symbol).bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lots,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": 20,
        "magic": 234000,
        "comment": "vanguard",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    return result
```

### MT5 Config
```json
{
    "mt5": {
        "login": "ENV:MT5_LOGIN",
        "password": "ENV:MT5_PASSWORD",
        "server": "ENV:MT5_SERVER",
        "timeout_seconds": 10,
        "magic_number": 234000,
        "deviation": 20,
        "comment_prefix": "vanguard"
    }
}
```

---

## Execution Pipeline (Per Cycle)

```python
def execute_cycle(portfolio, account):
    """
    For each approved trade in V6 output:
    1. Check execution is enabled for this account
    2. Build order payload (SignalStack JSON or MT5 request)
    3. Send order
    4. Log result (fill price, timestamp, order ID)
    5. Update portfolio state
    6. Send Telegram alert
    """
    if account.execution_mode == 'off':
        # Forward tracking only
        log_picks(portfolio)
        send_telegram_alert(portfolio, mode='picks_only')
        return

    for trade in portfolio:
        if trade.status != 'APPROVED':
            continue

        # Build and send order
        if account.prop_firm == 'ttp':
            result = send_signalstack_order(
                webhook_url=account.signalstack_webhook_url,
                symbol=trade.symbol,
                action=map_direction_to_action(trade.direction, 'open'),
                quantity=trade.shares_or_lots
            )
        elif account.prop_firm == 'ftmo':
            result = place_mt5_order(
                symbol=trade.symbol,
                direction=trade.direction,
                lots=trade.shares_or_lots,
                sl_price=trade.stop_price,
                tp_price=trade.tp_price
            )

        # Log execution result
        log_execution(trade, result)

        # Update portfolio state
        update_portfolio_state(account, trade, result)

    # Telegram summary
    send_telegram_alert(portfolio, mode='executed')
```

---

## EOD Flatten Execution

```python
def execute_eod_flatten(account):
    """
    At 15:50 ET, close ALL open positions for day-trade accounts.
    """
    if not account.rules.must_close_eod:
        return

    positions = get_open_positions(account.account_id)

    for pos in positions:
        if account.prop_firm == 'ttp':
            # SignalStack 'close' action handles direction automatically
            send_signalstack_order(
                webhook_url=account.signalstack_webhook_url,
                symbol=pos.symbol,
                action='close',
                quantity=pos.shares_or_lots
            )
        elif account.prop_firm == 'ftmo':
            close_mt5_position(pos)

    log_eod_flatten(account, positions)
    send_telegram_alert(positions, mode='eod_flatten')
```

---

## Telegram Alerts

### Alert Types
| Type | When | Content |
|---|---|---|
| `cycle_picks` | Every cycle (execution OFF) | Top 5 long + 5 short with edge scores |
| `new_entry` | Trade executed | Symbol, direction, shares, price, stop, TP |
| `trade_closed` | Position closed | Symbol, P&L, hold time |
| `eod_flatten` | 15:50 ET | All positions being closed |
| `daily_summary` | 16:05 ET | Day P&L, # trades, win rate, regime |
| `risk_alert` | Budget warning | Daily pause approaching, volume limit |
| `system_error` | Pipeline failure | Stage that failed, error message |

### Telegram Config
```json
{
    "telegram": {
        "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
        "chat_id": "ENV:TELEGRAM_CHAT_ID",
        "enabled": true,
        "alert_types": ["all"]
    }
}
```

---

## Session Lifecycle

### Startup (09:25 ET)
1. Load config
2. Initialize DB connections (vanguard_universe.db)
3. Assert DB isolation (not v2_universe.db)
4. Initialize portfolio state for each enabled account
5. Calculate today's daily pause level for TTP accounts
6. Verify data connections (Alpaca/IBKR, SignalStack, MT5)
7. Warm cache: fetch bars from 09:30-09:35

### Main Loop (09:35 - 15:25 ET, every 5 min)
1. Wait for bar completion (5m boundary)
2. Run V1→V2→V3→V4B→V5→V6
3. Execute trades (if enabled)
4. Update dashboard
5. Check for cycle lag (abort if > configured threshold)

### Wind-Down (15:30 - 16:05 ET)
1. 15:30 — Stop V5 from selecting new entries
2. 15:50 — Execute EOD flatten for day-trade accounts
3. 16:00 — Write session close report
4. 16:05 — Snapshot forward tracking results

---

## Failure Handling

### Hard Abort Session
- No market data bars for > 2 consecutive cycles
- DB unavailable or corrupt
- Cycle lag > 3× interval (15 min behind on 5m bars)
- Execution bridge returns repeated errors (3+ consecutive)

### Degraded Mode
- Model unavailable → skip V4B, use V5 edge=0 (no trades, just log)
- Factor engine late → skip V5/V6 for this cycle, retry next
- Risk state inconsistent → flatten all and stop

### Recovery
- Auto-restart via systemd/supervisord
- On restart, load portfolio state from DB
- Resume from current bar, don't replay missed cycles

---

## Output Contract

### Table: vanguard_execution_log

```sql
CREATE TABLE IF NOT EXISTS vanguard_execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_ts_utc TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    action TEXT NOT NULL,
    shares_or_lots REAL,
    order_type TEXT,
    limit_price REAL,
    stop_price REAL,
    fill_price REAL,
    fill_quantity REAL,
    execution_bridge TEXT,
    bridge_order_id TEXT,
    status TEXT,
    error_message TEXT,
    latency_ms INTEGER,
    created_at_utc TEXT DEFAULT (datetime('now'))
);
```

### Table: vanguard_session_log

```sql
CREATE TABLE IF NOT EXISTS vanguard_session_log (
    date TEXT PRIMARY KEY,
    session_start_utc TEXT,
    session_end_utc TEXT,
    total_cycles INTEGER DEFAULT 0,
    successful_cycles INTEGER DEFAULT 0,
    failed_cycles INTEGER DEFAULT 0,
    trades_executed INTEGER DEFAULT 0,
    trades_forward_tracked INTEGER DEFAULT 0,
    regime_summary TEXT,
    daily_pnl_by_account TEXT,
    errors TEXT,
    status TEXT DEFAULT 'ACTIVE'
);
```

---

## Configuration

### config/vanguard_orchestrator_config.json

```json
{
    "orchestrator": {
        "bar_interval": "5Min",
        "session_start_et": "09:35",
        "session_end_et": "15:25",
        "no_new_positions_after_et": "15:30",
        "eod_flatten_time_et": "15:50",
        "session_close_et": "16:00",
        "max_cycle_lag_seconds": 900,
        "cycle_timeout_seconds": 120
    },
    "execution": {
        "default_mode": "off",
        "require_confirmation_for_live": true
    },
    "signalstack": {
        "webhook_url": "ENV:SIGNALSTACK_WEBHOOK_URL",
        "timeout_seconds": 5,
        "max_retries": 2,
        "order_type": "market",
        "log_payloads": true
    },
    "mt5": {
        "login": "ENV:MT5_LOGIN",
        "password": "ENV:MT5_PASSWORD",
        "server": "ENV:MT5_SERVER",
        "magic_number": 234000,
        "deviation": 20
    },
    "telegram": {
        "bot_token": "ENV:TELEGRAM_BOT_TOKEN",
        "chat_id": "ENV:TELEGRAM_CHAT_ID",
        "enabled": true
    }
}
```

---

## File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_orchestrator.py        # V7 entry point
├── vanguard/
│   ├── helpers/
│   │   ├── clock.py                    # UTC/ET time, market sessions
│   │   ├── market_sessions.py          # Session boundaries, holidays
│   │   ├── db.py                       # SQLite WAL helpers
│   │   └── quality.py                  # NaN/outlier checks
│   ├── execution/
│   │   ├── bridge.py                   # Execution dispatcher
│   │   ├── signalstack_adapter.py      # SignalStack webhook client
│   │   ├── mt5_adapter.py              # MT5 API client
│   │   └── telegram_alerts.py          # Telegram bot notifications
│   └── __init__.py
├── config/
│   ├── vanguard_accounts.json          # From V6
│   ├── vanguard_orchestrator_config.json
│   └── vanguard_instrument_specs.json  # From V6
└── tests/
    ├── test_vanguard_orchestrator.py
    ├── test_signalstack_adapter.py
    └── test_mt5_adapter.py
```

---

## CLI

```bash
# Normal — full session loop
python3 stages/vanguard_orchestrator.py

# Single cycle (no loop)
python3 stages/vanguard_orchestrator.py --single-cycle

# Dry run (no execution, no DB writes)
python3 stages/vanguard_orchestrator.py --dry-run

# Override execution mode
python3 stages/vanguard_orchestrator.py --execution-mode paper
python3 stages/vanguard_orchestrator.py --execution-mode live

# Skip specific stages
python3 stages/vanguard_orchestrator.py --skip-execution

# Force EOD flatten now
python3 stages/vanguard_orchestrator.py --flatten-now ttp_daytrade_100k_flex

# Status check
python3 stages/vanguard_orchestrator.py --status
```

---

## Acceptance Criteria

- [ ] stages/vanguard_orchestrator.py exists and runs
- [ ] Market-hours loop: 09:35-15:25 ET every 5 minutes
- [ ] Calls V1→V2→V3→V4B→V5→V6 in sequence each cycle
- [ ] Session lifecycle: startup, main loop, wind-down
- [ ] No new positions after 15:30 ET
- [ ] EOD flatten at 15:50 ET for day-trade accounts
- [ ] Session summary at 16:00 ET
- [ ] Execution bridge: SignalStack adapter for TTP
- [ ] Execution bridge: MT5 adapter for FTMO
- [ ] Execution mode per account: off / paper / live
- [ ] Default execution mode: off
- [ ] SignalStack webhook payloads match documented format
- [ ] MT5 order placement via MetaTrader5 Python
- [ ] Telegram alerts for all event types
- [ ] Execution log table written
- [ ] Session log table written
- [ ] Forward tracking mode (execution off, picks logged)
- [ ] Failure handling: hard abort, degraded mode, recovery
- [ ] Cycle lag detection and alerting
- [ ] No imports from v2_*.py (Meridian)
- [ ] DB isolation: vanguard_universe.db only
- [ ] All timestamps UTC in storage, ET in display
- [ ] Config-driven (no hardcoded times, URLs, or credentials)

---

## What V7 Does NOT Do

- Chart rendering / UI (Meridian React app handles display)
- Model training (V4B)
- Feature engineering (V3)
- Data collection beyond V1 scope
- Position management beyond open/close (no scaling, no pyramiding in v1)
