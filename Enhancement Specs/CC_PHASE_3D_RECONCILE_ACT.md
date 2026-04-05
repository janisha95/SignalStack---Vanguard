# CC Phase 3d — Reconciliation Actions (DB-Only State Updates)

**Target env:** `Vanguard_QAenv`
**Est time:** 45 min – 1 hour
**Depends on:** Phase 3c PASS
**Risk level:** MEDIUM — mutates DB state based on detection, but NEVER sends broker commands
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

3c detects divergences. 3d acts on them — but **only by updating our DB**, never by sending commands to the broker. This enforces the "MetaApi wins" rule: when broker and DB disagree, we update DB to match broker. Never the reverse.

No auto-close actions in this slice. Those live in 3f.

---

## 1. Hard rules

1. QAenv only.
2. **Reconciler makes ZERO broker calls beyond the read from 3a.** No `close_position()`, no `place_order()`, no `modify_order()`. All commands to broker come from elsewhere.
3. Every DB mutation writes a reconciliation_log row with `resolved_action` set to the specific action taken (not `DETECTED_ONLY`).
4. All mutations happen in a single transaction per divergence. Either full commit or full rollback.
5. If resolution raises an exception mid-transaction, rollback and log with `resolved_action=RESOLUTION_FAILED`.

---

## 2. What to build

### 2.1 Extend `reconciler.py` with resolution functions

```python
def resolve_db_open_broker_closed(db_path, divergence: Divergence, broker_last_known_deal: dict | None):
    """
    Broker says position is gone. We need to close the journal row.
    Actions:
    - UPDATE vanguard_trade_journal SET
        status='RECONCILED_CLOSED',
        close_price=<from broker deal history if available, else null>,
        closed_at_utc=<from broker deal history if available, else now>,
        close_reason='RECONCILED',
        realized_pnl=<computed if deal available>,
        holding_minutes=<computed>
      WHERE trade_id=?
    - DELETE FROM vanguard_open_positions WHERE broker_position_id=?
    - resolved_action='JOURNAL_MARKED_CLOSED'
    """

def resolve_unknown_position(db_path, divergence: Divergence, broker_position: dict):
    """
    Broker has position, no journal record. Create a journal row with trade_id=null-ish.
    Actions:
    - INSERT INTO vanguard_trade_journal with:
        trade_id='recon_' + broker_position_id (prefix to distinguish from uuid trade_ids)
        profile_id=...
        symbol, side, approved_qty=qty, expected_entry=entry_price
        approval_reasoning_json='{"origin":"reconciled_unknown"}'
        broker_position_id=..., fill_price=entry_price, fill_qty=qty
        status='OPEN'
        approved_cycle_ts_utc=<now>
        notes='Position discovered via reconciliation, no system approval record.'
    - UPSERT INTO vanguard_open_positions
    - resolved_action='JOURNAL_ROW_INSERTED'
    """

def resolve_qty_mismatch(db_path, divergence: Divergence, broker_position: dict):
    """
    Broker qty differs from DB. Update DB to broker truth.
    Actions:
    - UPDATE vanguard_open_positions SET qty=<broker qty>
    - Add note to journal row: 'qty updated via reconciliation from X to Y'
    - DO NOT change the original approved_qty on journal (that's historical truth)
    - resolved_action='DB_QTY_SYNCED'
    """

def resolve_db_closed_broker_open(db_path, divergence: Divergence, broker_position: dict):
    """
    Rare case: journal says closed, but broker still has position.
    This is dangerous — means a previous close attempt silently failed.
    Actions:
    - DO NOT reopen the journal row (preserves audit trail of what we thought happened)
    - INSERT a new journal row with trade_id='recon_reopen_' + broker_position_id
    - Flag via Telegram alert (Shan must investigate)
    - resolved_action='FLAGGED_FOR_MANUAL_REVIEW'
    """
```

### 2.2 Main resolve dispatcher

```python
def resolve_divergences(db_path, divergences: list[Divergence], broker_positions: list[dict]):
    """
    For each divergence, dispatch to the correct resolver.
    Wraps each resolution in a try/except + transaction.
    On exception: log with resolved_action='RESOLUTION_FAILED' + error message.
    """
```

### 2.3 Broker deal history helper (optional best-effort)

If MetaApi SDK supports fetching deal history: `MetaApiClient.get_last_deal(position_id)` returns the close event. Used by `resolve_db_open_broker_closed` to populate close_price/closed_at_utc accurately. If unavailable, fallback to null with note `"close_price_unknown_from_reconciliation"`.

### 2.4 Extend `reconcile_once.py`

Add `--act` flag. Without flag: behaves as 3c (detect only). With flag: detects AND resolves.

```
python3 scripts/reconcile_once.py --profile-id gft_10k_qa --act
```

Always safer to dry-run first:
```
python3 scripts/reconcile_once.py --profile-id gft_10k_qa          # detect only
python3 scripts/reconcile_once.py --profile-id gft_10k_qa --act    # detect + resolve
```

---

## 3. Acceptance tests

### Test 1 — No-op when states agree
Ensure clean state (no open positions, clean DB). Run `reconcile_once.py --act`.
**Expect:** 0 divergences, 0 DB mutations, 0 new reconciliation_log rows.

### Test 2 — DB_OPEN_BROKER_CLOSED resolved
Place a test trade → journal=OPEN + open_positions has row. Manually close the position in MT5. Fetch once to update mirror (still has row — fetch doesn't remove it between closes). Actually, fetch_once DOES remove closed positions (slice 3a Test 3 proved that). So run `reconcile_once --act` WITHOUT a prior fetch.

Workflow:
1. Place test trade via orchestrator, status=OPEN
2. Close manually in MT5
3. Run `reconcile_once.py --profile-id gft_10k_qa --act`

**Expect:**
- journal row status=RECONCILED_CLOSED
- open_positions row for this trade DELETED
- reconciliation_log row with resolved_action=JOURNAL_MARKED_CLOSED
- close_price populated (if broker deal history available)

### Test 3 — UNKNOWN_POSITION resolved
Manually open a position in MT5 (no orchestrator approval). Run `reconcile_once --act`.
**Expect:**
- New journal row with trade_id starting with 'recon_'
- status=OPEN
- approval_reasoning_json has `{"origin":"reconciled_unknown"}`
- reconciliation_log row with resolved_action=JOURNAL_ROW_INSERTED

### Test 4 — QTY_MISMATCH resolved
Place test trade qty=0.10 → journal + open_positions row. Manually add a second 0.05 position via MT5 for same symbol (broker will show as separate position, not aggregated, **so this may not trigger QTY_MISMATCH if MetaApi treats them as separate positions**).

**Alternative test:** Directly UPDATE `vanguard_open_positions.qty` to a wrong value. Run reconcile --act.
**Expect:**
- open_positions qty updated back to broker truth
- reconciliation_log row with resolved_action=DB_QTY_SYNCED

### Test 5 — DB_CLOSED_BROKER_OPEN handling
This is hard to reproduce naturally. Simulate:
1. Place test trade (journal=OPEN, open_positions populated).
2. Manually UPDATE `vanguard_trade_journal SET status='CLOSED'` for this trade_id.
3. Run reconcile --act.

**Expect:**
- Original journal row NOT modified (still status=CLOSED)
- New journal row inserted with trade_id starting with 'recon_reopen_'
- Telegram alert fires with "DB_CLOSED_BROKER_OPEN for {symbol}"
- reconciliation_log row with resolved_action=FLAGGED_FOR_MANUAL_REVIEW

### Test 6 — Transactional rollback on failure
Temporarily break `vanguard_open_positions` (rename column to force failure). Run reconcile --act with a DB_OPEN_BROKER_CLOSED case.
**Expect:** journal row NOT mutated (rollback), reconciliation_log has resolved_action=RESOLUTION_FAILED with exception message.
Restore schema.

### Test 7 — Idempotency
Run `reconcile_once.py --act` twice in a row with no broker state changes.
**Expect:** second run finds 0 divergences, 0 new mutations, 0 new log rows.

### Test 8 — Dry-run (no --act) still works as 3c
```bash
python3 scripts/reconcile_once.py --profile-id gft_10k_qa  # no --act
```
**Expect:** detections logged with resolved_action=DETECTED_ONLY, no state mutations.

### Test 9 — Audit trail complete
After Tests 2–5, check:
```sql
SELECT divergence_type, resolved_action, COUNT(*) FROM vanguard_reconciliation_log GROUP BY 1,2;
```
**Expect:** one row per (type, action) pair matching tests performed.

---

## 4. Report-back criteria

CC produces:
1. New code files + updated reconcile_once.py
2. Test 1–9 output verbatim
3. For Test 2 + 3 + 5: show before/after journal rows
4. Confirmation: grep for `close_position\|place_order\|modify_order` in reconciler.py returns zero matches

---

## 5. Stop-the-line triggers

- Reconciler makes any broker write/close call
- DB mutation occurs without matching reconciliation_log row
- Transaction rollback doesn't work (partial state persists on failure)
- Idempotency fails (repeated runs keep adding log rows)
- Original journal row gets modified in DB_CLOSED_BROKER_OPEN case (must preserve audit trail)
