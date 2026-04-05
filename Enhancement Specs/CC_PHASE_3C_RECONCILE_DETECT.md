# CC Phase 3c — Reconciliation Engine (DETECT-ONLY, No State Mutation)

**Target env:** `Vanguard_QAenv`
**Est time:** 30–45 min
**Depends on:** Phase 3a + 3b PASS
**Risk level:** LOW — pure diagnostic, logs divergences but does NOT act on them
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

Before we let code **act** on DB/broker divergences, we need to verify it **detects** them correctly. This slice runs the reconciler in observe-only mode: it compares broker state vs DB state, writes a detailed log of every divergence, and does **not mutate anything**.

This is the foundation for 3d (acting) and 3e (running in a loop). If detection is wrong here, acting on it later is dangerous.

---

## 1. Hard rules

1. QAenv only.
2. **ZERO mutations.** This slice does not UPDATE or INSERT or DELETE anything except in `vanguard_reconciliation_log`.
3. Every detected divergence writes exactly one log row with full detail.
4. No `fix`, `repair`, or `reconcile` side effects. Those live in 3d.
5. One-shot CLI, not a daemon. Daemon comes in 3e.

---

## 2. What to build

### 2.1 DB table: `vanguard_reconciliation_log` (new)

```sql
CREATE TABLE IF NOT EXISTS vanguard_reconciliation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  detected_at_utc TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  divergence_type TEXT NOT NULL,
  broker_position_id TEXT,
  db_trade_id TEXT,
  symbol TEXT,
  details_json TEXT NOT NULL,
  resolved_action TEXT NOT NULL DEFAULT 'DETECTED_ONLY'
);
CREATE INDEX IF NOT EXISTS idx_reconlog_profile_time ON vanguard_reconciliation_log(profile_id, detected_at_utc);
```

**Divergence types (4 known):**
- `DB_OPEN_BROKER_CLOSED` — our `vanguard_open_positions` has a row, broker doesn't
- `DB_CLOSED_BROKER_OPEN` — broker has position, our `vanguard_trade_journal` shows status=CLOSED or no matching trade_id
- `QTY_MISMATCH` — both sides have the position but qty differs (broker may have partial fill or manual size change)
- `UNKNOWN_POSITION` — broker has position, no linked trade_id in our journal (e.g., Shan manually opened it)

For this slice, `resolved_action` is always `DETECTED_ONLY`.

### 2.2 File: `Vanguard_QAenv/vanguard/execution/reconciler.py` (new)

```python
@dataclass(frozen=True)
class Divergence:
    divergence_type: str
    profile_id: str
    broker_position_id: str | None
    db_trade_id: str | None
    symbol: str | None
    details: dict

def detect_divergences(db_path, profile_id, broker_positions, db_open_positions, db_journal_open) -> list[Divergence]:
    """
    Pure function. No I/O except reads.
    Inputs:
      broker_positions: list of dicts from MetaApiClient.get_open_positions()
      db_open_positions: list of rows from vanguard_open_positions for profile
      db_journal_open: list of journal rows where status=OPEN for profile
    Returns list of Divergence objects.
    """

def log_divergences(db_path, divergences: list[Divergence], detected_at_utc: str):
    """Inserts one row per divergence. Only touches vanguard_reconciliation_log."""
```

### 2.3 Divergence detection logic

```
For each broker_position BP:
  match = find in db_open_positions where broker_position_id == BP.broker_position_id
  if not match:
    → DB_CLOSED_BROKER_OPEN (or UNKNOWN_POSITION, see below)
  elif abs(match.qty - BP.qty) > 1e-8:
    → QTY_MISMATCH
  # else: they agree, no divergence

For each db_open_position DOP:
  match = find in broker_positions where broker_position_id == DOP.broker_position_id
  if not match:
    → DB_OPEN_BROKER_CLOSED

To distinguish DB_CLOSED_BROKER_OPEN vs UNKNOWN_POSITION:
  journal_row = find in vanguard_trade_journal where broker_position_id == BP.broker_position_id
  if journal_row exists:
    → DB_CLOSED_BROKER_OPEN (we thought it was closed but broker says open)
  else:
    → UNKNOWN_POSITION (position was never journaled — manual trade)
```

### 2.4 CLI script: `Vanguard_QAenv/scripts/reconcile_once.py` (new)

Usage:
```
python3 scripts/reconcile_once.py --profile-id gft_10k_qa
```

Behavior:
1. Load runtime config.
2. Create MetaApiClient, call `get_open_positions()`.
3. Load `vanguard_open_positions` rows for profile.
4. Load `vanguard_trade_journal` rows where status=OPEN for profile.
5. Call `detect_divergences()`.
6. Print summary table of divergences to stdout.
7. Call `log_divergences()` to persist.
8. Exit 0 regardless of divergence count (detection is always expected to work).
9. Exit 2 if MetaApi unavailable.

---

## 3. Acceptance tests

### Test 1 — No divergences when states agree
Ensure paper account has 0 positions and DB has 0 rows. Run `reconcile_once.py`.
**Expect:** "0 divergences detected", 0 new rows in reconciliation_log.

### Test 2 — DB_OPEN_BROKER_CLOSED detected
Broker: 0 positions. Manually INSERT fake row into `vanguard_open_positions` for profile. Run reconcile.
**Expect:** 1 divergence logged, divergence_type=DB_OPEN_BROKER_CLOSED, fake row still present (NOT mutated).
Then DELETE the fake row manually.

### Test 3 — UNKNOWN_POSITION detected
Manually open a position directly in MT5 UI (or via a direct MetaApi SDK call). Run `metaapi_fetch_once.py` to update mirror. Run reconcile.
**Expect:** 1 divergence logged as DB_OPEN_BROKER_CLOSED? No wait — after 3a's fetch, `vanguard_open_positions` has the row. But `vanguard_trade_journal` has no matching `broker_position_id`. So journal_open list doesn't contain it.

Let me re-trace: the detection logic as specified would say "broker has it + DB mirror has it" → no divergence there. But comparing broker to journal: broker has it, journal doesn't → UNKNOWN_POSITION.

**Refined detection:** the comparison should be `broker_positions` vs `db_journal_open` (journal is authoritative record), with `db_open_positions` being the broker mirror. Actually let me simplify:

Detection compares:
- broker_positions (truth from MetaApi)
- db_journal_open (what our system THINKS is open, by trade journal)

`vanguard_open_positions` is just a cache, not authoritative.

Rewriting detection:
```
For each broker_position BP:
  j = find journal row where broker_position_id == BP.broker_position_id AND status==OPEN
  if j is None:
    → UNKNOWN_POSITION (broker has it, we have no open journal record)
  elif abs(j.approved_qty - BP.qty) > 1e-8:
    → QTY_MISMATCH

For each db_journal_open J:
  bp = find in broker_positions where broker_position_id == J.broker_position_id
  if bp is None:
    → DB_OPEN_BROKER_CLOSED (journal says open, broker doesn't have it — likely closed by TP/SL/manual)
```

**Use this refined logic.** `vanguard_open_positions` is just a read-cache, not used for detection.

**Test 3 actual expectation:** manual position opened in MT5 → no journal row exists → UNKNOWN_POSITION logged.

### Test 4 — QTY_MISMATCH detected
Place a test trade via orchestrator → journal row created with qty=0.10. Then manually modify the position size in MT5 (or place a 2nd partial order). Run reconcile.
**Expect:** 1 divergence logged, divergence_type=QTY_MISMATCH, details_json contains both qty values.

### Test 5 — DB_OPEN_BROKER_CLOSED detected
Place a test trade, get it journaled as OPEN. Then close it manually in MT5. Run reconcile.
**Expect:** 1 divergence logged, divergence_type=DB_OPEN_BROKER_CLOSED. Journal row **still status=OPEN** (not mutated).

### Test 6 — No state mutation
After running all above tests, verify:
- No UPDATE to vanguard_trade_journal (use `SELECT MAX(last_synced_at_utc) FROM vanguard_trade_journal;` before and after)
- No row change count on vanguard_open_positions (from Test 2)
- Only vanguard_reconciliation_log has new rows

Script check:
```bash
sqlite3 qa.db "SELECT COUNT(*), COUNT(CASE WHEN resolved_action='DETECTED_ONLY' THEN 1 END) FROM vanguard_reconciliation_log;"
```
**Expect:** both counts equal (100% DETECTED_ONLY).

### Test 7 — Details JSON is parseable
```sql
SELECT details_json FROM vanguard_reconciliation_log ORDER BY id DESC LIMIT 5;
```
**Expect:** each row's details_json parses as valid JSON.

### Test 8 — MetaApi outage doesn't create phantom divergences
Break MetaApi creds. Run reconcile.
**Expect:** exit 2, zero new divergence_log rows.

---

## 4. Report-back criteria

CC produces:
1. `reconciler.py`, `reconcile_once.py`, table DDL
2. Test 1–8 output verbatim
3. Sample divergence_log rows as JSON (one per divergence type)
4. `diff` output showing zero changes to vanguard_trade_journal during all tests

---

## 5. Stop-the-line triggers

- Any test shows a state mutation outside reconciliation_log
- Divergence type misclassified (e.g., manual trade logged as QTY_MISMATCH instead of UNKNOWN_POSITION)
- MetaApi outage creates fake divergences
