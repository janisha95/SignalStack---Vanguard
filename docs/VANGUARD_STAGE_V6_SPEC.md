# STAGE V6: Vanguard Risk Filters — vanguard_risk_filters.py

**Status:** APPROVED  
**Project:** Vanguard  
**Date:** Mar 29, 2026  
**Depends on:** V5 (shortlist) + live portfolio state  
**Informed by:** TTP Trader Evolution platform inspection (Chrome MCP, Mar 28 2026)

---

## What Stage V6 Does

Takes the ranked shortlist from V5 and produces a sized, compliant
tradeable portfolio FOR EACH active prop firm account.

Same shortlist in. Different portfolio out per account. Each account
has its own rules, its own balance, its own position limits, its own
daily P&L tracking.

---

## Multi-Account Architecture

```
V5 Shortlist (15 LONG + 15 SHORT)
        │
        ├──→ Account: TTP Day Trade $100K  ──→ Portfolio A
        ├──→ Account: FTMO $100K           ──→ Portfolio B
        └──→ (future accounts added via JSON config)
```

Each account is independent. They share the same shortlist but apply
different filters, different sizing, different rules. A trade that
passes TTP rules might fail FTMO rules.

To add a new account: add an entry to the JSON config. No code changes.

---

## TTP Platform Fields (Captured from Trader Evolution)

The following fields were captured directly from TTP's Trader Evolution
platform via Chrome MCP inspection on Mar 28, 2026. These are the EXACT
field names the platform uses:

### Account Panel — General
| Field | Description |
|---|---|
| Balance | Current account balance |
| Projected balance | Balance including unrealized P&L |
| Balance & All risks | Balance accounting for all risk exposure |
| Available funds | Funds available for new positions |
| Blocked balance | Margin blocked by open positions |
| Cash balance | Cash portion of balance |
| Open gross P/L | Unrealized gross P&L |
| Open net P/L | Unrealized net P&L |
| # Positions | Current open position count |
| # Orders | Current working order count |
| Total positions value | Notional value of all positions |

### Account Panel — Today Results
| Field | Description |
|---|---|
| Today's gross | Gross P&L today |
| Today's net | Net P&L today (after fees) |
| Today's fees | Fees paid today |
| Today's volume | Total share volume today |
| # Today's trades | Number of trades executed today |
| Today's rebates | ECN rebates earned today |

### Account Panel — Risk Management (SERVER-ENFORCED)
| Field | Description |
|---|---|
| Trading status | Active / Paused / Terminated |
| Daily loss limit | current / maximum (e.g. 0.00 / 10,000.00 USD) |
| Weekly loss limit | current / maximum (e.g. 0.00 / 50,000.00 USD) |
| Equities volume limit | current / maximum (e.g. 0.00 / 1,000,000.00 USD) |

### Platform Warnings (Client-side configurable)
| Warning | Default |
|---|---|
| Order quantity exceeds | 100 shares |
| Order capital exceeds | 2,000,000.00 USD |
| Today's gross less than | 0.00 |
| Available funds less than | 1.00 |
| Today's volume exceeds | 1 |
| Symbol halt warnings | Available |
| Off-market order warning | Enabled |

### Trading Defaults
| Setting | Default |
|---|---|
| Order type | Market |
| Market validity | Day |
| Stop/Limit validity | GTC |

---

## TTP $100K Day Trade Account Rules (from tradethepool.com)

| Rule | Flex | Max |
|---|---|---|
| Buying Power | $100,000 | $100,000 |
| Profit Target | 6% ($6,000) | 6% ($6,000) |
| Daily Pause | 2% ($2,000) | 1% ($1,000) |
| Max Loss (Drawdown) | 4% ($4,000) | 3% ($3,000) |
| Minimum Positions | 10 | 20 |
| Trading Period | Unlimited | 60 days |
| Payout Split | 70/30 | 70/30 |
| Min Trade Duration | 30 seconds | 30 seconds |
| Min Trade Range | 10 cents | 10 cents |
| Consistency Rule | 50% | 30% (eval only) |
| Earnings Overnight | Prohibited | Prohibited |
| Max Position Profit | 30% of total valid profit | 30% |

### Scaling (Day Trade)
- 10% profit target → Buying Power +5%, Daily Pause +10%
- Example: $100K → $5K profit (5%) → not yet scaled
- Example: $100K → $10K profit (10%) → New BP $105K, Daily Pause $2,200

### Daily Pause Mechanics (CRITICAL)
- Calculated at START of trading day
- Remains FIXED for that day regardless of intraday P&L changes
- When realized+unrealized profit > 3× Daily Loss → Max Loss resets to initial balance
- Example: $100K account, $2K daily pause → if profit hits $6K, max loss moves to $100K

---

## Account Configuration

### config/vanguard_accounts.json

```json
{
    "accounts": [
        {
            "account_id": "ttp_daytrade_100k_flex",
            "enabled": true,
            "prop_firm": "ttp",
            "account_type": "day_trade",
            "plan_type": "flex",
            "account_balance": 100000,
            "currency": "USD",
            "instruments": "us_equities",
            "rules": {
                "profit_target_pct": 0.06,
                "daily_pause_pct": 0.02,
                "max_loss_pct": 0.04,
                "min_positions_for_target": 10,
                "risk_per_trade_pct": 0.004,
                "max_positions": 10,
                "max_trades_per_day": 30,
                "max_per_sector": 2,
                "max_correlation": 0.85,
                "max_portfolio_heat_pct": 0.015,
                "max_single_position_profit_pct": 0.30,
                "consistency_rule_pct": 0.50,
                "stop_atr_multiple": 1.5,
                "tp_atr_multiple": 3.0,
                "must_close_eod": true,
                "no_new_positions_after": "15:30",
                "eod_flatten_time": "15:50",
                "block_earnings_overnight": true,
                "block_halted": true,
                "min_trade_duration_sec": 30,
                "min_trade_range_cents": 10,
                "equities_volume_limit": 1000000,
                "scaling": {
                    "profit_trigger_pct": 0.10,
                    "bp_increase_pct": 0.05,
                    "daily_pause_increase_pct": 0.10
                }
            }
        },
        {
            "account_id": "ftmo_100k",
            "enabled": true,
            "prop_firm": "ftmo",
            "account_type": "swing",
            "plan_type": "standard",
            "account_balance": 100000,
            "currency": "USD",
            "instruments": "all_ftmo",
            "rules": {
                "profit_target_pct": 0.10,
                "daily_loss_pct": 0.05,
                "max_loss_pct": 0.10,
                "risk_per_trade_pct": 0.005,
                "max_positions": 10,
                "max_trades_per_day": 30,
                "max_per_sector": 3,
                "max_correlation": 0.85,
                "max_portfolio_heat_pct": 0.04,
                "stop_atr_multiple": 2.0,
                "tp_atr_multiple": 4.0,
                "must_close_eod": false,
                "no_new_positions_after": null,
                "eod_flatten_time": null,
                "best_day_max_pct": 0.50,
                "min_trading_days": 4
            }
        }
    ]
}
```

---

## Risk Filter Pipeline (Per Account)

```
For each enabled account:

Step 1: INSTRUMENT FILTER
  - TTP: only us_equities from shortlist
  - FTMO: only ftmo instruments from shortlist

Step 2: DAILY BUDGET CHECK (TTP: "Daily Pause")
  daily_budget = (daily_pause_pct × account_balance) - abs(today_realized_loss)
  IF daily_budget <= 0 → account PAUSED for today, no new trades

Step 3: MAX LOSS CHECK (TTP: "Max Loss" / drawdown)
  drawdown_budget = (max_loss_pct × account_balance) - abs(max_drawdown_to_date)
  IF drawdown_budget <= 0 → account TERMINATED, alert

Step 4: WEEKLY LOSS CHECK (TTP only)
  weekly_budget = weekly_loss_limit - abs(week_realized_loss)
  IF weekly_budget <= 0 → account PAUSED for week

Step 5: EQUITIES VOLUME CHECK (TTP only)
  IF today_volume >= equities_volume_limit → no new trades

Step 6: TIME GATE
  IF must_close_eod AND current_time > no_new_positions_after → no new
  IF current_time > eod_flatten_time → flatten all open positions

Step 7: POSITION LIMIT
  IF open_positions >= max_positions → no new trades

Step 8: CONSISTENCY CHECK (TTP)
  IF max_single_position_profit / total_profit > max_single_position_profit_pct
  → flag for review (no single trade > 30% of total profit)

Step 9: POSITION SIZING (per candidate, in V5 rank order)
  For each candidate (best edge first):
    intraday_atr = ATR(5m bars, 14 period)
    stop_distance = intraday_atr × stop_atr_multiple
    risk_dollars = min(
        risk_per_trade_pct × account_balance,
        daily_budget × 0.5,
        drawdown_budget × 0.25
    )
    IF direction == LONG:
        shares = floor(risk_dollars / stop_distance)
        stop_price = price - stop_distance
        tp_price = price + (intraday_atr × tp_atr_multiple)
    IF direction == SHORT:
        shares = floor(risk_dollars / stop_distance)
        stop_price = price + stop_distance
        tp_price = price - (intraday_atr × tp_atr_multiple)

    # TTP min trade range check
    IF abs(tp_price - price) < min_trade_range_cents/100 → skip

Step 10: DIVERSIFICATION FILTERS
  a. Sector cap: max N per sector (count existing + new)
  b. Correlation cap: reject if corr > threshold with existing position
  c. Heat cap: reject if total portfolio heat exceeds max

Step 11: PROP-FIRM SPECIFIC RULES
  TTP: block_earnings_overnight, block_halted, min_trade_duration
  FTMO: best_day_max_pct check, min_trading_days

Step 12: SCALING CHECK (TTP)
  IF total_profit >= scaling.profit_trigger_pct × account_balance:
    new_bp = account_balance × (1 + scaling.bp_increase_pct)
    new_daily_pause = daily_pause × (1 + scaling.daily_pause_increase_pct)
    → update account config

Step 13: WRITE
  Write tradeable portfolio for this account
  Update portfolio state for this account
```

---

## Output Contract

### Table: vanguard_tradeable_portfolio

```sql
CREATE TABLE IF NOT EXISTS vanguard_tradeable_portfolio (
    cycle_ts_utc TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL,
    stop_price REAL,
    tp_price REAL,
    shares_or_lots REAL,
    risk_dollars REAL,
    risk_pct REAL,
    position_value REAL,
    intraday_atr REAL,
    edge_score REAL,
    rank INTEGER,
    status TEXT DEFAULT 'APPROVED',
    rejection_reason TEXT,
    PRIMARY KEY (cycle_ts_utc, account_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_vg_tp_account
    ON vanguard_tradeable_portfolio(account_id, cycle_ts_utc);
```

### Table: vanguard_portfolio_state (per account)

```sql
CREATE TABLE IF NOT EXISTS vanguard_portfolio_state (
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    open_positions INTEGER DEFAULT 0,
    trades_today INTEGER DEFAULT 0,
    daily_realized_pnl REAL DEFAULT 0.0,
    daily_unrealized_pnl REAL DEFAULT 0.0,
    weekly_realized_pnl REAL DEFAULT 0.0,
    total_pnl REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    heat_used_pct REAL DEFAULT 0.0,
    best_day_pnl REAL DEFAULT 0.0,
    todays_volume INTEGER DEFAULT 0,
    todays_fees REAL DEFAULT 0.0,
    daily_pause_level REAL,
    max_loss_level REAL,
    status TEXT DEFAULT 'ACTIVE',
    last_updated_utc TEXT,
    PRIMARY KEY (account_id, date)
);
```

### Status Values
- ACTIVE — normal trading
- DAILY_PAUSED — daily loss pause reached (TTP term), no new trades today
- WEEKLY_PAUSED — weekly loss limit reached, no new trades this week
- MAX_LOSS — max drawdown reached, account terminated
- EOD_FLATTEN — flattening all positions
- VOLUME_LIMIT — equities volume limit reached for day
- PAUSED — manually paused

---

## Sizing: Equities vs Forex vs Futures

### Equities (TTP)
```python
shares = floor(risk_dollars / stop_distance_per_share)
position_value = shares × price
# Check against equities_volume_limit
```

### Forex (FTMO)
```python
pip_value = lot_size × pip_size
lots = risk_dollars / (stop_distance_pips × pip_value_per_lot)
```

### Futures
```python
contracts = floor(risk_dollars / (stop_distance_ticks × tick_value))
```

Instrument specs loaded from config/vanguard_instrument_specs.json

---

## EOD Flatten Logic (TTP Day Trade)

```python
def check_eod_flatten(account, current_time_et):
    if not account.rules.must_close_eod:
        return None
    
    if current_time_et >= account.rules.eod_flatten_time:
        return {
            'action': 'FLATTEN_ALL',
            'reason': f'EOD flatten at {account.rules.eod_flatten_time}',
            'positions': get_open_positions(account.account_id)
        }
    
    if current_time_et >= account.rules.no_new_positions_after:
        return {
            'action': 'NO_NEW_TRADES',
            'reason': f'No new positions after {account.rules.no_new_positions_after}'
        }
    
    return None
```

---

## TTP Daily Pause Implementation

```python
def calculate_daily_pause(account, start_of_day_balance):
    """
    TTP Daily Pause is calculated at START of trading day.
    Remains FIXED for the entire day.
    """
    daily_pause_level = start_of_day_balance - (
        account.rules.daily_pause_pct * account.account_balance
    )
    return daily_pause_level

def check_scaling_reset(account, current_pnl):
    """
    When profit exceeds 3× Daily Loss, max loss resets to initial balance.
    """
    daily_loss_amount = account.rules.daily_pause_pct * account.account_balance
    if current_pnl >= 3 * daily_loss_amount:
        return account.account_balance  # max loss now at initial balance
    return None
```

---

## Performance Target

| Metric | Target |
|---|---|
| V6 compute time (per account) | < 2 seconds |
| V6 total (all accounts) | < 10 seconds |

---

## File Structure

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_risk_filters.py       # V6 entry point
├── vanguard/
│   ├── helpers/
│   │   ├── position_sizer.py          # Equity/forex/futures sizing
│   │   ├── portfolio_state.py         # Per-account state management
│   │   ├── correlation_checker.py     # Pairwise correlation
│   │   ├── eod_flatten.py            # EOD logic for intraday accounts
│   │   └── ttp_rules.py              # TTP-specific: daily pause, scaling, consistency
│   └── __init__.py
├── config/
│   ├── vanguard_accounts.json         # All prop firm accounts
│   └── vanguard_instrument_specs.json # Tick values, lot sizes, pip sizes
└── tests/
    └── test_vanguard_risk.py
```

---

## CLI

```bash
# Normal — process all enabled accounts
python3 stages/vanguard_risk_filters.py

# Specific account
python3 stages/vanguard_risk_filters.py --account ttp_daytrade_100k_flex

# Dry run
python3 stages/vanguard_risk_filters.py --dry-run

# Debug one symbol through all accounts
python3 stages/vanguard_risk_filters.py --debug AAPL

# Force EOD flatten
python3 stages/vanguard_risk_filters.py --flatten ttp_daytrade_100k_flex

# Show current portfolio state
python3 stages/vanguard_risk_filters.py --status
```

---

## Acceptance Criteria

- [ ] stages/vanguard_risk_filters.py exists and runs
- [ ] Multi-account: processes each enabled account independently
- [ ] Same shortlist, different portfolio per account
- [ ] Account config loaded from JSON (no code changes to add accounts)
- [ ] TTP Daily Pause check (calculated at start of day, fixed)
- [ ] TTP Max Loss (drawdown) check
- [ ] TTP Weekly Loss check
- [ ] TTP Equities Volume Limit check
- [ ] TTP Scaling logic (profit trigger → BP increase + daily pause increase)
- [ ] TTP Consistency rule (no single trade > 30% of total profit)
- [ ] TTP Min trade duration (30 sec) tracked
- [ ] TTP Min trade range (10 cents) enforced
- [ ] Position sizing: ATR-based for equities, pip-based for forex, tick-based for futures
- [ ] Sector concentration cap
- [ ] Correlation cap
- [ ] Portfolio heat cap
- [ ] Time gate: no new positions after cutoff
- [ ] EOD flatten for day trade accounts
- [ ] TTP earnings overnight block
- [ ] TTP halted stock block
- [ ] FTMO best day cap check
- [ ] Writes vanguard_tradeable_portfolio per account
- [ ] Writes vanguard_portfolio_state per account
- [ ] Rejection reasons logged for every filtered candidate
- [ ] < 10 seconds total for all accounts
- [ ] No imports from v2_*.py (Meridian)

---

## Out of Scope

- Execution / order placement (V7)
- Signal generation (V3-V5)
- Data intake (V1)
- Broker API interaction (execution layer)
- P&L tracking from live fills (execution layer)
