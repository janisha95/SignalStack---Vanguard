# CLAUDE CODE — Build V6 Risk Filters
# Date: Mar 31, 2026
# Spec: VANGUARD_STAGE_V6_SPEC.md
# Depends on: V5 (shortlist) + account_profiles table (already built in Session 5)
# IMPORTANT: V6 must USE the account_profiles table from the unified API, not duplicate config

---

## READ FIRST

```bash
# 1. Full V6 spec
cat ~/SS/Meridian/VANGUARD_STAGE_V6_SPEC.md 2>/dev/null || \
  find ~/SS -name "VANGUARD_STAGE_V6_SPEC.md" 2>/dev/null

# 2. Account profiles ALREADY EXIST — Session 5 built this
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema account_profiles"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT id, name, prop_firm, account_type, account_size, daily_loss_limit, max_drawdown, max_positions, must_close_eod FROM account_profiles"

# 3. V5 shortlist table (V5 should be built by now)
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db ".schema vanguard_shortlist" 2>/dev/null || echo "V5 not built yet"
sqlite3 ~/SS/Vanguard/data/vanguard_universe.db "SELECT COUNT(*) FROM vanguard_shortlist" 2>/dev/null

# 4. Existing V6 files
ls ~/SS/Vanguard/stages/vanguard_risk_filters.py 2>/dev/null || echo "V6 doesn't exist yet"
ls ~/SS/Vanguard/vanguard/helpers/position_sizer.py 2>/dev/null || echo "No position sizer yet"

# 5. What account profiles are seeded
curl -s http://localhost:8090/api/v1/accounts 2>/dev/null | python3 -c "
import sys, json
accs = json.load(sys.stdin)
for a in accs:
    print(f'{a[\"id\"]:25s} {a[\"name\"]:25s} \${a[\"account_size\"]:>10,} DLL=\${a[\"daily_loss_limit\"]:>6,} DD=\${a[\"max_drawdown\"]:>6,} pos={a[\"max_positions\"]} eod={a[\"must_close_eod\"]}')
" 2>/dev/null || echo "API not running"
```

Report ALL output before writing code.

---

## CRITICAL: USE EXISTING ACCOUNT PROFILES TABLE

Session 5 already built `account_profiles` in `vanguard_universe.db` with 6 profiles:

| ID | Name | Size | DLL | Max DD | Max Pos | EOD |
|---|---|---|---|---|---|---|
| ttp_20k_swing | TTP 20K Swing | $20,000 | $600 | $1,400 | 5 | No |
| ttp_40k_swing | TTP 40K Swing | $40,000 | $1,200 | $2,800 | 5 | No |
| ttp_50k_intraday | TTP 50K Intraday | $50,000 | $1,000 | $2,000 | 5 | Yes |
| ttp_100k_intraday | TTP 100K Intraday | $100,000 | $2,000 | $4,000 | 5 | Yes |
| topstep_100k | TopStep 100K | $100,000 | $2,000 | $3,000 | 10 | Yes |
| ftmo_100k | FTMO 100K | $100,000 | $5,000 | $10,000 | 10 | No |

V6 MUST read from this table — not from a separate `vanguard_accounts.json`.
The spec references `vanguard_accounts.json` but the actual implementation
should use the DB table (which the UI can also manage via Settings page).

To extend the schema if needed (for TTP-specific rules like scaling, weekly
loss, volume limit, consistency rule), add columns to account_profiles:

```sql
-- Check what columns already exist
PRAGMA table_info(account_profiles);

-- Add any missing columns needed for V6
ALTER TABLE account_profiles ADD COLUMN risk_per_trade_pct REAL DEFAULT 0.005;
ALTER TABLE account_profiles ADD COLUMN max_per_sector INTEGER DEFAULT 3;
ALTER TABLE account_profiles ADD COLUMN max_correlation REAL DEFAULT 0.85;
ALTER TABLE account_profiles ADD COLUMN max_portfolio_heat_pct REAL DEFAULT 0.04;
ALTER TABLE account_profiles ADD COLUMN stop_atr_multiple REAL DEFAULT 1.5;
ALTER TABLE account_profiles ADD COLUMN tp_atr_multiple REAL DEFAULT 3.0;
ALTER TABLE account_profiles ADD COLUMN no_new_positions_after TEXT;  -- "15:30" or null
ALTER TABLE account_profiles ADD COLUMN eod_flatten_time TEXT;         -- "15:50" or null
ALTER TABLE account_profiles ADD COLUMN consistency_rule_pct REAL;     -- 0.30 for TTP
ALTER TABLE account_profiles ADD COLUMN weekly_loss_limit REAL;
ALTER TABLE account_profiles ADD COLUMN volume_limit REAL;
ALTER TABLE account_profiles ADD COLUMN scaling_profit_trigger_pct REAL;
ALTER TABLE account_profiles ADD COLUMN scaling_bp_increase_pct REAL;
ALTER TABLE account_profiles ADD COLUMN scaling_pause_increase_pct REAL;
```

Only add columns that don't already exist. Use PRAGMA to check first.

---

## WHAT TO BUILD

### Entry point: stages/vanguard_risk_filters.py

For EACH active account profile in `account_profiles`:
1. Load V5 shortlist for current cycle
2. Filter instruments by prop firm type (TTP = US equities only, FTMO = all)
3. Check daily budget, drawdown budget, weekly budget
4. Check time gates (no new positions after cutoff, EOD flatten)
5. Check position limits
6. Size each candidate: ATR-based stops, shares from risk/stop distance
7. Apply diversification filters (sector cap, correlation, heat)
8. Apply prop-firm-specific rules (TTP scaling, consistency, min duration)
9. Write to vanguard_tradeable_portfolio
10. Update vanguard_portfolio_state

### File structure:

```
~/SS/Vanguard/
├── stages/
│   └── vanguard_risk_filters.py       # V6 entry point
├── vanguard/
│   ├── helpers/
│   │   ├── position_sizer.py          # ATR-based sizing (equity/forex/futures)
│   │   ├── portfolio_state.py         # Per-account state (reads/writes vanguard_portfolio_state)
│   │   ├── correlation_checker.py     # Pairwise 60-day rolling correlation
│   │   ├── eod_flatten.py            # EOD logic for intraday accounts
│   │   └── ttp_rules.py              # TTP-specific: daily pause, scaling, consistency
```

### Position sizing:

```python
def size_equity_position(price, atr_14, stop_atr_mult, risk_dollars, direction):
    stop_distance = atr_14 * stop_atr_mult
    shares = math.floor(risk_dollars / stop_distance)
    if direction == "LONG":
        stop_price = round(price - stop_distance, 2)
        tp_price = round(price + atr_14 * tp_atr_mult, 2)
    else:
        stop_price = round(price + stop_distance, 2)
        tp_price = round(price - atr_14 * tp_atr_mult, 2)
    return shares, stop_price, tp_price

# Risk dollars = min(
#   risk_per_trade_pct × account_size,
#   daily_budget_remaining × 0.5,
#   drawdown_budget_remaining × 0.25
# )
```

### ATR source:
Use the `/api/v1/sizing/{symbol}/{direction}` endpoint already built,
OR compute ATR(14) directly from daily_bars in the DB. Don't duplicate
the sizing logic — reuse what exists.

---

## OUTPUT TABLES

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

---

## TESTS

```
tests/test_vanguard_risk.py:
1. ATR-based position sizing produces correct shares
2. Daily budget check pauses account when exceeded
3. Max loss check terminates account when exceeded
4. Position limit prevents new trades when at max
5. Time gate blocks trades after cutoff
6. EOD flatten returns all open positions for closure
7. Sector cap filters excess positions in same sector
8. Correlation cap blocks highly correlated positions
9. TTP consistency rule flags single-trade dominance
10. Each account gets independent portfolio (same shortlist, different output)
11. Account profiles loaded from DB (not hardcoded)
12. Missing/disabled account skipped gracefully
```

---

## CLI

```bash
python3 stages/vanguard_risk_filters.py                                    # all accounts
python3 stages/vanguard_risk_filters.py --account ttp_50k_intraday         # specific account
python3 stages/vanguard_risk_filters.py --dry-run                          # no DB writes
python3 stages/vanguard_risk_filters.py --debug AAPL                       # trace one symbol
python3 stages/vanguard_risk_filters.py --flatten ttp_100k_intraday        # force EOD flatten
python3 stages/vanguard_risk_filters.py --status                           # show portfolio state
```

---

## VERIFY

```bash
python3 -m py_compile ~/SS/Vanguard/stages/vanguard_risk_filters.py
cd ~/SS/Vanguard && python3 -m pytest tests/test_vanguard_risk.py -v
python3 stages/vanguard_risk_filters.py --dry-run --account ttp_50k_intraday
```

---

## GIT

```bash
cd ~/SS/Vanguard && git add -A && git commit -m "feat: V6 Risk Filters — multi-account, ATR sizing, prop firm rules"
```
