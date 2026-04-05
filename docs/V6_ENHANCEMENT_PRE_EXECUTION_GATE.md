# V6 ENHANCEMENT SPEC — Pre-Execution Risk Gate
# Prevent account blowups like the TTP Demo incident (Mar 30, 2026)
# Date: Mar 31, 2026
# For: Claude Code — integrate into existing V6 risk_filters.py

---

## Why This Exists

On Mar 30, 2026, 30 market orders were placed on a TTP Demo Swing 1M account.
TTP force-liquidated all 30 positions at market open, resulting in -$15,188 loss
and account disablement. The risk filters (V6) existed but were NOT wired into
the execution path — trades went directly from Trade Desk → SignalStack webhook
with zero risk checks.

This enhancement ensures V6 risk checks run BEFORE any trade reaches the webhook.

---

## Architecture: Pre-Execution Gate

```
Trade Desk "Execute All" button
        │
        ▼
POST /api/v1/execute
        │
        ▼
┌─────────────────────────────┐
│ V6 PRE-EXECUTION RISK GATE  │  ← NEW: runs BEFORE webhook
│                              │
│ For EACH trade in the batch: │
│ 1. Load account profile      │
│ 2. Check portfolio state      │
│ 3. Apply ALL V6 filters       │
│ 4. REJECT trades that fail    │
│ 5. APPROVE trades that pass   │
│                              │
│ Returns: approved[], rejected[]│
│ Only approved go to webhook   │
└─────────────────────────────┘
        │
        ├─── approved[] → SignalStack webhook
        │
        └─── rejected[] → returned to UI with reasons
```

---

## Risk Checks (in order, ALL must pass)

### Check 1: Account Active
```python
if profile.status == "disabled":
    reject("Account is disabled")
```

### Check 2: Daily Loss Budget
```python
# Get current daily P&L from portfolio state OR from live portfolio endpoint
daily_pnl = get_daily_pnl(profile.id)  # realized + unrealized
daily_pause = profile.daily_loss_limit

if abs(daily_pnl) >= daily_pause * 0.70:
    reject(f"Daily loss at {abs(daily_pnl)/daily_pause*100:.0f}% of limit — headroom too low")

# CRITICAL: 70% threshold, not 100%. Leave 30% headroom for closing positions.
```

### Check 3: Max Drawdown Budget
```python
total_pnl = get_total_pnl(profile.id)  # from portfolio state
max_dd = profile.max_drawdown

if abs(total_pnl) >= max_dd * 0.80:
    reject(f"Drawdown at {abs(total_pnl)/max_dd*100:.0f}% of limit")
```

### Check 4: Position Count
```python
open_positions = get_open_position_count(profile.id)
new_trades = len(approved_so_far)

if open_positions + new_trades >= profile.max_positions:
    reject(f"Position limit: {open_positions} open + {new_trades} queued ≥ {profile.max_positions} max")
```

### Check 5: Single Position Concentration
```python
# No single position > 10% of account (configurable per profile)
max_single_pct = 0.10  # default 10%
position_value = trade.shares * trade.entry_price

if position_value > profile.account_size * max_single_pct:
    reject(f"Position {trade.symbol} = ${position_value:,.0f} ({position_value/profile.account_size*100:.1f}% of account) exceeds {max_single_pct*100:.0f}% limit")
```

### Check 6: Total Batch Exposure
```python
# Total new exposure from this batch must not exceed 50% of account
batch_exposure = sum(t.shares * t.entry_price for t in approved_so_far)
max_batch_pct = 0.50  # 50% of account

if batch_exposure > profile.account_size * max_batch_pct:
    reject(f"Batch exposure ${batch_exposure:,.0f} exceeds {max_batch_pct*100:.0f}% of ${profile.account_size:,.0f} account")
```

### Check 7: Total Portfolio Risk
```python
# Total risk across ALL positions (existing + new) must not exceed daily pause
existing_risk = sum(abs(p.entry - p.stop) * p.shares for p in open_positions)
new_risk = abs(trade.entry_price - trade.stop_loss) * trade.shares
total_risk = existing_risk + new_risk

if total_risk > profile.daily_loss_limit * 0.80:
    reject(f"Total portfolio risk ${total_risk:,.0f} exceeds 80% of ${profile.daily_loss_limit:,.0f} daily pause")
```

### Check 8: Duplicate Symbol
```python
# Don't open a second position in the same symbol + direction
existing_symbols = {(p.symbol, p.direction) for p in open_positions}
if (trade.symbol, trade.direction) in existing_symbols:
    reject(f"Already have {trade.direction} position in {trade.symbol}")
```

### Check 9: Sector Concentration
```python
sector = get_sector(trade.symbol)
sector_count = count_positions_in_sector(profile.id, sector)
max_per_sector = profile.max_per_sector or 3

if sector_count >= max_per_sector:
    reject(f"Sector {sector}: {sector_count} positions ≥ {max_per_sector} max")
```

### Check 10: Prop Firm Specific Rules
```python
if profile.prop_firm == "ttp":
    # TTP: min trade duration 30 seconds
    # (can't enforce pre-execution, but log it for monitoring)

    # TTP: min trade range $0.10
    if abs(trade.entry_price - trade.stop_loss) < 0.10:
        reject(f"Stop distance ${abs(trade.entry_price - trade.stop_loss):.2f} below TTP $0.10 minimum range")

    # TTP: consistency rule — no single trade > 50% of total profit
    # (can't enforce pre-execution for future trades, but warn)

    # TTP: earnings overnight block (for swing accounts)
    # Check if symbol reports earnings tonight/tomorrow
```

---

## API Contract Change

### POST /api/v1/execute — Enhanced Response

```json
{
  "approved": [
    {"id": 42, "symbol": "JPM", "direction": "LONG", "status": "SUBMITTED", "webhook_response": "OK"}
  ],
  "rejected": [
    {"symbol": "ULCC", "direction": "LONG", "reason": "Position $53,000 = 5.3% of account exceeds 10% limit", "check": "single_position_concentration"},
    {"symbol": "NODE", "direction": "LONG", "reason": "Daily loss at 75% of limit — headroom too low", "check": "daily_loss_budget"}
  ],
  "summary": {
    "submitted": 3,
    "rejected": 2,
    "total_risk": 1500.00,
    "total_exposure": 45000.00,
    "account_profile": "ttp_40k_swing"
  }
}
```

### Frontend: Show rejected trades in execution confirmation

Before executing, show the user which trades will be rejected and why:

```
⚠️ 2 of 5 trades will be blocked by risk filters:

  ULCC LONG — Position 5.3% of account (max 10%)
  NODE LONG — Daily loss at 75% of limit

3 trades will be submitted:
  JPM LONG 47 shares
  SHO LONG 2777 shares
  TRIN LONG 930 shares

[Cancel] [Execute 3 Approved]
```

---

## Account Profile Enhancements

Add these fields to `account_profiles` table (if not already present):

```sql
ALTER TABLE account_profiles ADD COLUMN max_single_position_pct REAL DEFAULT 0.10;
ALTER TABLE account_profiles ADD COLUMN max_batch_exposure_pct REAL DEFAULT 0.50;
ALTER TABLE account_profiles ADD COLUMN dll_headroom_pct REAL DEFAULT 0.70;
ALTER TABLE account_profiles ADD COLUMN dd_headroom_pct REAL DEFAULT 0.80;
ALTER TABLE account_profiles ADD COLUMN max_per_sector INTEGER DEFAULT 3;
ALTER TABLE account_profiles ADD COLUMN block_duplicate_symbols INTEGER DEFAULT 1;
```

Update the 6 seeded profiles with TTP-specific values:

| Profile | Max Single Pos | Max Batch | DLL Headroom | Sector Cap |
|---|---|---|---|---|
| TTP 20K Swing | 15% | 60% | 70% | 2 |
| TTP 40K Swing | 12% | 50% | 70% | 3 |
| TTP 50K Intraday | 10% | 40% | 65% | 2 |
| TTP 100K Intraday | 8% | 40% | 65% | 3 |
| TopStep 100K | 10% | 50% | 70% | 3 |
| FTMO 100K | 8% | 40% | 70% | 3 |

---

## SignalStack Action Fix (TTP)

TTP only supports 4 actions: `buy`, `sell`, `cancel`, `close`.
There is NO `sell_short` or `buy_to_cover` for TTP.

Fix in `signalstack_adapter.py`:

```python
# For TTP:
#   open LONG  → "buy"
#   open SHORT → "sell"  (TTP treats sell without position as short)
#   close any  → "close"
```

---

## Implementation Order

1. Fix `sell_short` → `sell` for TTP in signalstack_adapter.py
2. Add pre-execution risk gate function in trade_desk.py
3. Wire risk gate into POST /api/v1/execute (before webhook call)
4. Add new columns to account_profiles
5. Update seeded profiles with risk parameters
6. Update frontend execution confirmation to show approved/rejected
7. Test end-to-end: queue 10 trades, verify risk gate rejects overconcentrated ones

---

## What This Would Have Prevented (Mar 30 Incident)

With these checks active on the TTP 1M Demo account:
- Check 4 (Position Count): Only 5-10 of 30 trades would pass (max_positions)
- Check 5 (Single Position): ULCC at $53K (5.3% of $1M) would pass, but on smaller accounts it would be rejected
- Check 6 (Batch Exposure): 30 trades totaling $500K+ would exceed 50% batch limit
- Check 7 (Portfolio Risk): Combined risk of 30 positions would exceed daily pause headroom
- Result: ~5 well-sized trades would execute, not 30 concentrated bets
