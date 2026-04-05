# CC Phase 3 — Position Lifecycle Daemon, MetaApi Sync, Auto-Close, Trade Journal, Slippage

**Target env:** `Vanguard_QAenv`
**Est time:** 2–2.5 hours
**Depends on:** Phase 2b PASS (policy engine working, account_state table exists)
**Prod-path touches:** NONE. Phase 3 runs against a **separate GFT paper/demo account** — not touching the live GFT 5k/10k accounts Shan is manually trading on.

---

## 0. Why this exists

V6 approves an entry, executor sends it — and then the system forgets. We need to know: did it open? is it still open? hit TP/SL? exceeded max hold? Without this, automation is half-built and the UI lies.

**MetaApi is source of truth.** DB mirrors it. On disagreement, reconcile to MetaApi. Never the other way.

---

## 1. Hard rules

1. QAenv only. Uses a **dedicated GFT paper account** in MetaApi config, never the live 5k/10k accounts.
2. Lifecycle daemon is **independent of shortlist loop**. Runs on its own timer even when no candidates exist.
3. **MetaApi wins every disagreement.** Reconcile-on-read: if DB says position open but MetaApi says closed, update DB + log reconciliation event.
4. **Hard close at max holding minutes + Telegram notification.** Both, not one or the other.
5. Slippage captured at fill, not inferred later.
6. Trade journal is the **per-trade lifecycle record** — different grain from execution_log (which is per-order-event).
7. If MetaApi is unreachable, daemon logs error and **does not write speculative state**. No "last known good" without timestamp+flag.

---

## 2. What to build

### 2.1 New DB tables

```sql
-- Per-trade lifecycle record (replaces partial coverage in execution_log)
CREATE TABLE IF NOT EXISTS vanguard_trade_journal (
  trade_id TEXT PRIMARY KEY,             -- uuid generated at approval
  profile_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  asset_class TEXT NOT NULL,
  side TEXT NOT NULL,                    -- LONG | SHORT
  -- Approval phase
  approved_cycle_ts_utc TEXT NOT NULL,
  approved_qty REAL NOT NULL,
  expected_entry REAL NOT NULL,
  expected_sl REAL NOT NULL,
  expected_tp REAL NOT NULL,
  approval_reasoning_json TEXT NOT NULL, -- full PolicyDecision JSON
  spread_pct_at_approval REAL,
  -- Execution phase
  broker_order_id TEXT,
  broker_position_id TEXT,
  submitted_at_utc TEXT,
  fill_price REAL,
  fill_qty REAL,
  filled_at_utc TEXT,
  slippage_price REAL,                   -- fill_price - expected_entry (signed, signed to side)
  slippage_bps REAL,                     -- slippage_price / expected_entry * 10000
  commission REAL,
  -- Lifecycle phase
  status TEXT NOT NULL,                  -- PENDING_FILL | OPEN | CLOSED | REJECTED_BY_BROKER | RECONCILED_CLOSED
  close_price REAL,
  closed_at_utc TEXT,
  close_reason TEXT,                     -- TP_HIT | SL_HIT | AUTO_CLOSED_MAX_HOLD | MANUAL_CLOSE | RECONCILED
  realized_pnl REAL,
  realized_rrr REAL,                     -- (|close-entry|/|sl-entry|) signed
  holding_minutes INTEGER,
  last_synced_at_utc TEXT NOT NULL,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_profile_status ON vanguard_trade_journal(profile_id, status);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON vanguard_trade_journal(symbol);

-- Live mirror of MetaApi open positions (reconciled each poll)
CREATE TABLE IF NOT EXISTS vanguard_open_positions (
  profile_id TEXT NOT NULL,
  broker_position_id TEXT NOT NULL,
  trade_id TEXT,                         -- link to trade_journal (may be null if position opened outside our system)
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  entry_price REAL NOT NULL,
  current_sl REAL,
  current_tp REAL,
  opened_at_utc TEXT NOT NULL,
  unrealized_pnl REAL,
  last_synced_at_utc TEXT NOT NULL,
  PRIMARY KEY (profile_id, broker_position_id)
);

-- Reconciliation event log — every time DB state diverged from MetaApi
CREATE TABLE IF NOT EXISTS vanguard_reconciliation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_ts_utc TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  divergence_type TEXT NOT NULL,         -- DB_OPEN_BROKER_CLOSED | DB_CLOSED_BROKER_OPEN | QTY_MISMATCH | UNKNOWN_POSITION
  details_json TEXT NOT NULL,
  resolved_action TEXT NOT NULL          -- DB_UPDATED_TO_CLOSED | DB_INSERTED_NEW | FLAGGED_FOR_REVIEW
);
```

### 2.2 File: `Vanguard_QAenv/vanguard/execution/metaapi_client.py` (new or extend)

Thin wrapper over the MetaApi Python SDK. Exports:

```python
class MetaApiClient:
    def __init__(self, account_id: str, api_key: str, base_url: str): ...

    async def get_open_positions(self) -> list[dict]:
        """Returns [{position_id, symbol, side, qty, entry_price, sl, tp, open_time, unrealized_pnl}, ...]"""

    async def get_account_state(self) -> dict:
        """Returns {equity, balance, margin, free_margin}"""

    async def close_position(self, position_id: str) -> dict:
        """Returns {success, close_price, close_time, realized_pnl}"""

    async def place_order(self, symbol, side, qty, sl, tp) -> dict:
        """Returns {order_id, submitted_at, status}"""
```

If MetaApi unreachable: raise `MetaApiUnavailable`. Do not swallow.

### 2.3 File: `Vanguard_QAenv/vanguard/execution/lifecycle_daemon.py` (new)

The independent lifecycle service. Runs on its own interval (default 60s), independent of orchestrator cycle.

```python
class LifecycleDaemon:
    def __init__(self, config, db_path): ...

    async def run_loop(self):
        while True:
            for profile in active_profiles:
                try:
                    await self.sync_profile(profile)
                except MetaApiUnavailable as e:
                    log_error_with_timestamp(profile.id, e)
                    # do NOT update DB state speculatively
                    continue
            await asyncio.sleep(self.interval_s)

    async def sync_profile(self, profile):
        # 1. Fetch MetaApi open positions
        broker_positions = await client.get_open_positions()
        # 2. Fetch DB open positions
        db_positions = load_open_positions(profile.id)
        # 3. Reconcile (see §2.4)
        reconcile(profile.id, broker_positions, db_positions, cycle_ts)
        # 4. Update vanguard_open_positions from broker truth
        upsert_open_positions(profile.id, broker_positions)
        # 5. Check for positions exceeding max_holding_minutes
        auto_close_check(profile, broker_positions)
        # 6. Update account_state from MetaApi
        state = await client.get_account_state()
        upsert_account_state(profile.id, state)
```

### 2.4 File: `Vanguard_QAenv/vanguard/execution/reconciler.py` (new)

```python
def reconcile(profile_id, broker_positions, db_positions, cycle_ts_utc):
    """
    For each divergence:
    - DB open, broker closed -> fetch last deal from broker, update journal (status=CLOSED,
      close_price, close_reason=RECONCILED if no prior close_reason), log reconciliation event
    - DB closed, broker open -> insert new journal row with status=OPEN and trade_id=null, flag for review
    - qty mismatch -> update DB qty to broker qty, log event
    - Position in broker with no DB record -> insert journal row with trade_id=null (manual trade
      or pre-existing), log UNKNOWN_POSITION event
    """
```

### 2.5 File: `Vanguard_QAenv/vanguard/execution/auto_close.py` (new)

```python
def auto_close_check(profile, policy, broker_positions, client, cycle_ts_utc):
    """
    For each broker position:
    - Compute holding_minutes = (cycle_ts - opened_at)
    - If holding_minutes >= policy.position_limits.max_holding_minutes
      AND policy.position_limits.auto_close_after_max_holding:
        - Send Telegram alert: "Closing {symbol} on {profile.id} — held {holding_minutes}m"
        - Call client.close_position(position_id)
        - On success: update journal with close_reason=AUTO_CLOSED_MAX_HOLD, close_price, closed_at_utc
        - Send Telegram confirmation with realized P&L
        - On failure: send Telegram ALERT "Auto-close FAILED for {symbol} — manual intervention required"
    """
```

**Both close attempt AND notification fire. Not one or the other.**

### 2.6 Wire into execution path: update `_execute_approved()` in orchestrator

When a candidate is submitted to MetaApi:
1. Generate `trade_id = uuid4()`.
2. Insert trade_journal row immediately with status=PENDING_FILL, approval fields populated.
3. After submit, record `broker_order_id`, `submitted_at_utc`, `spread_pct_at_approval`.
4. On fill: update row with fill_price, fill_qty, filled_at_utc, **slippage_price**, **slippage_bps**, status=OPEN, broker_position_id.
5. Compute slippage signed to side:
   - LONG: `slippage = fill_price - expected_entry` (positive = worse)
   - SHORT: `slippage = expected_entry - fill_price` (positive = worse)

### 2.7 Lifecycle daemon startup: bash script

`Vanguard_QAenv/scripts/start_lifecycle_daemon.sh`:

```bash
#!/bin/bash
cd /Users/sjani008/SS/Vanguard_QAenv
nohup python3 -m vanguard.execution.lifecycle_daemon \
  --interval 60 \
  --log /tmp/lifecycle_daemon.log \
  > /tmp/lifecycle_daemon.stdout 2>&1 &
echo $! > /tmp/lifecycle_daemon.pid
echo "Lifecycle daemon started, PID $(cat /tmp/lifecycle_daemon.pid)"
```

### 2.8 Telegram notifications

New alert types:
- `LIFECYCLE_AUTO_CLOSE_INITIATED` — pre-close warning
- `LIFECYCLE_AUTO_CLOSE_SUCCESS` — post-close with P&L
- `LIFECYCLE_AUTO_CLOSE_FAILED` — requires manual intervention
- `LIFECYCLE_RECONCILIATION_DIVERGENCE` — DB/broker disagreed, what we did
- `LIFECYCLE_METAAPI_UNAVAILABLE` — fired once per profile per outage (not per poll)

---

## 3. Acceptance tests

### Test 1 — Daemon starts, polls, writes
Start daemon. Wait 90s. Check `vanguard_open_positions` has fresh `last_synced_at_utc` for each active profile.
**Expect:** rows present, timestamps < 90s old.

### Test 2 — Slippage captured
Place a test market order through `_execute_approved()`. Check `vanguard_trade_journal` row.
**Expect:** `fill_price`, `slippage_price`, `slippage_bps` populated and non-zero.

### Test 3 — Reconciliation: DB open, broker closed
Manually insert a fake `vanguard_open_positions` row for a position that doesn't exist in broker. Wait one daemon cycle.
**Expect:** row removed from open_positions, journal updated to CLOSED with close_reason=RECONCILED, reconciliation_log has DB_OPEN_BROKER_CLOSED event.

### Test 4 — Reconciliation: broker has unknown position
Manually open a trade directly in MT5 (bypass our system). Wait one daemon cycle.
**Expect:** new row in open_positions + journal (trade_id=null), reconciliation_log has UNKNOWN_POSITION event.

### Test 5 — Auto-close at max hold
Set `policy.position_limits.max_holding_minutes=5` for gft_10k. Place a test order. Wait 6 minutes (use daemon interval=30s for testing).
**Expect:** position closed in broker, journal updated with close_reason=AUTO_CLOSED_MAX_HOLD, **two Telegram messages** (initiated + success).

### Test 6 — MetaApi outage handled safely
Break MetaApi credentials (wrong account_id). Run daemon.
**Expect:** error logged, no DB writes, Telegram alert LIFECYCLE_METAAPI_UNAVAILABLE fires exactly once (not per poll).

### Test 7 — Journal row immutability after close
Attempt to UPDATE a journal row where status=CLOSED. This should succeed (no DB-level trigger), but the daemon must never update a CLOSED row. Verify daemon code has explicit check.
**Expect:** grep shows guard `if status == "CLOSED": continue` in reconciler.

### Test 8 — Independence of shortlist loop
Stop orchestrator (kill the main cycle). Keep daemon running. Open a manual trade.
**Expect:** daemon still syncs, journal still updates. Lifecycle does NOT require orchestrator running.

### Test 9 — Realized RRR computed correctly
Close a position profitably. Check journal.
**Expect:** `realized_rrr = |close - fill| / |sl - fill|`, with sign = +1 if profitable, -1 if losing.

### Test 10 — holding_minutes correct
For a position open 3h17m, journal should show `holding_minutes = 197`.

---

## 4. Report-back criteria

CC must produce:
1. New/edited file list with line counts
2. Output of Tests 1–10 verbatim
3. Sample row from `vanguard_trade_journal` (one complete lifecycle: approval → fill → close) as JSON
4. Sample `vanguard_reconciliation_log` row for each divergence type tested
5. Screenshot or text log of Telegram notifications received during Test 5
6. Confirmation: daemon ran for ≥ 10 minutes with no errors

---

## 5. Non-goals (this phase)

- UI for journal (Phase 5)
- Cross-profile correlation / heat rebalancing
- Automatic SL/TP modification post-entry (trailing stops)
- MetaApi account provisioning (assumes account exists)

---

## 6. Stop-the-line triggers

- Daemon writes to DB while MetaApi is unreachable
- Auto-close attempts close without both Telegram messages firing
- Reconciliation modifies MetaApi (daemon must only READ from broker, CLOSE is explicit action)
- Journal row double-updates (race condition)
- Slippage captured as unsigned or wrong sign for SHORT
