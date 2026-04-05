# CC Phase 3b — Trade Journal + Slippage Logging at Execution

**Target env:** `Vanguard_QAenv`
**Est time:** 30–45 min
**Depends on:** Phase 3a PASS
**Risk level:** MEDIUM — wires new logging into the live execution path, but only adds data, doesn't change decisions
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

We need per-trade lifecycle records **before** we can reconcile or auto-close anything. This slice wires trade_id generation and slippage capture into `_execute_approved()` so that every approved+submitted trade writes a journal row. No reconciliation, no closing. Just journaling.

---

## 1. Hard rules

1. QAenv only.
2. **No behavior changes in _execute_approved() except adding journal writes.** Same approval logic, same submit logic, same forward-tracking. Only difference: journal rows get populated.
3. Journal is **append-once on approval**, **updated-once on fill**. Never updated again by this slice.
4. Slippage captured at fill only. If fill data doesn't arrive (test mode, simulator), slippage stays null.
5. `trade_id` is a fresh UUID4 per approval. Never reused.

---

## 2. What to build

### 2.1 DB table: `vanguard_trade_journal` (new)

```sql
CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
  trade_id TEXT PRIMARY KEY,
  profile_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  asset_class TEXT NOT NULL,
  side TEXT NOT NULL,
  -- Approval phase
  approved_cycle_ts_utc TEXT NOT NULL,
  approved_qty REAL NOT NULL,
  expected_entry REAL NOT NULL,
  expected_sl REAL NOT NULL,
  expected_tp REAL NOT NULL,
  approval_reasoning_json TEXT NOT NULL,
  spread_pct_at_approval REAL,
  -- Execution phase
  broker_order_id TEXT,
  broker_position_id TEXT,
  submitted_at_utc TEXT,
  fill_price REAL,
  fill_qty REAL,
  filled_at_utc TEXT,
  slippage_price REAL,
  slippage_bps REAL,
  commission REAL,
  -- Lifecycle phase (populated by slices 3d/3f — stays null here)
  status TEXT NOT NULL DEFAULT 'PENDING_FILL',
  close_price REAL,
  closed_at_utc TEXT,
  close_reason TEXT,
  realized_pnl REAL,
  realized_rrr REAL,
  holding_minutes INTEGER,
  last_synced_at_utc TEXT NOT NULL,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_profile_status ON vanguard_trade_journal(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON vanguard_trade_journal(symbol);
```

### 2.2 File: `Vanguard_QAenv/vanguard/execution/trade_journal.py` (new)

```python
import uuid

def insert_approval_row(db_path, profile_id, candidate, policy_decision, cycle_ts_utc) -> str:
    """
    Inserts a row with status=PENDING_FILL.
    Returns the generated trade_id.
    Fields populated: trade_id, profile_id, symbol, asset_class, side,
      approved_cycle_ts_utc, approved_qty, expected_entry/sl/tp,
      approval_reasoning_json (full PolicyDecision as JSON),
      spread_pct_at_approval (from candidate).
    """

def update_submitted(db_path, trade_id, broker_order_id, submitted_at_utc):
    """Updates broker_order_id, submitted_at_utc. Requires status=PENDING_FILL."""

def update_filled(db_path, trade_id, broker_position_id, fill_price, fill_qty,
                  filled_at_utc, expected_entry, side, commission=None):
    """
    Updates fill fields + computes slippage.
    Slippage math (signed — positive means WORSE for us):
      if side == "LONG":  slippage_price = fill_price - expected_entry
      if side == "SHORT": slippage_price = expected_entry - fill_price
      slippage_bps = slippage_price / expected_entry * 10000
    Sets status=OPEN.
    Requires status=PENDING_FILL.
    """

def update_rejected_by_broker(db_path, trade_id, rejection_reason):
    """Sets status=REJECTED_BY_BROKER. Requires status=PENDING_FILL."""
```

All functions MUST enforce `status` precondition — if row is not in expected state, raise `JournalStateError`. No silent updates.

### 2.3 Wire into orchestrator: `Vanguard_QAenv/stages/vanguard_orchestrator.py` (edit)

Find `_execute_approved()`. For each approved candidate:

```python
# Before: directly submit to executor
# After:
trade_id = insert_approval_row(DB_PATH, profile_id, candidate, policy_decision, cycle_ts_utc)
candidate["_trade_id"] = trade_id  # carry forward to executor

# existing submit code
result = await executor.submit(candidate)

if result.success:
    update_submitted(DB_PATH, trade_id, result.broker_order_id, result.submitted_at_utc)
    # If fill is synchronous (some brokers): update_filled now.
    # If fill is async: slice 3e's daemon picks it up via broker_position_id match.
    if result.filled_immediately:
        update_filled(DB_PATH, trade_id, result.broker_position_id, result.fill_price,
                      result.fill_qty, result.filled_at_utc, candidate["entry"], candidate["side"])
else:
    update_rejected_by_broker(DB_PATH, trade_id, result.reason)
```

In `execution_mode=manual` or `test`: still call `insert_approval_row()` so we have the record, but skip submit. Set status manually to `FORWARD_TRACKED` (new status value, not in OPEN flow).

### 2.4 Extend schema to accept `FORWARD_TRACKED` status

Add to status values allowed: `PENDING_FILL`, `OPEN`, `CLOSED`, `REJECTED_BY_BROKER`, `FORWARD_TRACKED`, `RECONCILED_CLOSED`.

No CHECK constraint in SQLite (performance), but document allowed values in `trade_journal.py`:

```python
VALID_STATUSES = {"PENDING_FILL", "OPEN", "CLOSED", "REJECTED_BY_BROKER", "FORWARD_TRACKED", "RECONCILED_CLOSED"}
```

And enforce in every writer function.

---

## 3. Acceptance tests

### Test 1 — Dry-run cycle populates journal
Run orchestrator in `execution_mode=test` on a cycle that produces approvals. After cycle:
```sql
SELECT trade_id, profile_id, symbol, side, status, expected_entry, expected_sl, expected_tp
FROM vanguard_trade_journal WHERE approved_cycle_ts_utc = '<cycle_ts>';
```
**Expect:** one row per approved candidate, status=FORWARD_TRACKED, all expected_* fields populated, approval_reasoning_json is valid JSON matching the PolicyDecision.

### Test 2 — Manual test submit populates fill + slippage
Place a manual trade via `_execute_approved()` with `execution_mode=live` on the paper account. Wait for fill.
```sql
SELECT trade_id, fill_price, slippage_price, slippage_bps, status FROM vanguard_trade_journal WHERE trade_id = '<id>';
```
**Expect:** status=OPEN, fill_price populated, slippage_price non-null.

### Test 3 — Slippage sign correctness (LONG)
Simulate/fake a LONG fill with expected_entry=100.00, fill_price=100.05.
**Expect:** slippage_price = +0.05 (positive = worse fill for long).

### Test 4 — Slippage sign correctness (SHORT)
Simulate a SHORT fill with expected_entry=100.00, fill_price=100.05.
**Expect:** slippage_price = -0.05 → wait, let me re-check.

For SHORT: we sell at expected_entry=100, broker fills at 100.05 → we got a BETTER fill (higher sell price) → slippage should be negative (good).
Formula: `slippage_price = expected_entry - fill_price = 100.00 - 100.05 = -0.05` → negative = better for us ✓

For SHORT with fill_price=99.95: `100.00 - 99.95 = +0.05` → positive = worse for us ✓

**Expect:** slippage sign matches intuition above.

### Test 5 — State precondition enforced
Try to call `update_filled()` on a row where status=CLOSED.
**Expect:** raises `JournalStateError`, row unchanged.

### Test 6 — No duplicate trade_ids
Run 3 cycles producing ~10 approvals each. Check:
```sql
SELECT trade_id, COUNT(*) FROM vanguard_trade_journal GROUP BY trade_id HAVING COUNT(*) > 1;
```
**Expect:** zero rows.

### Test 7 — slippage_bps calculation
For Test 2 fill, verify `slippage_bps = slippage_price / expected_entry * 10000`.

### Test 8 — Reasoning JSON round-trip
```sql
SELECT approval_reasoning_json FROM vanguard_trade_journal LIMIT 1;
```
**Expect:** output parses as JSON with fields matching the PolicyDecision dataclass.

---

## 4. Report-back criteria

CC produces:
1. Files created/edited + line counts
2. Test 1–8 output verbatim
3. Sample journal row as JSON pretty-printed
4. Diff of `_execute_approved()` showing only journal additions, no behavior changes

---

## 5. Stop-the-line triggers

- Any approved candidate in a cycle produces no journal row
- Slippage sign wrong for SHORT
- State precondition not enforced (silent update succeeds)
- Existing forward-tracking or Telegram behavior breaks
- Duplicate trade_ids possible
