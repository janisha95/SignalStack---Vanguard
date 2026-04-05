# CC Phase 3a — MetaApi Read-Only Client + Open Positions Mirror

**Target env:** `Vanguard_QAenv`
**Est time:** 30–45 min
**Depends on:** Phase 2b PASS
**Risk level:** LOW — pure read, no writes to broker, no auto-close
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

Before we do anything with MetaApi data, we need to prove we can **read it correctly and handle outages safely**. This slice does nothing but fetch open positions and account state, write them to a mirror table, and verify the round-trip. If this slice is wrong, every later slice builds on garbage.

No daemon yet. No reconciliation. No closing. Just READ.

---

## 1. Hard rules

1. QAenv only. Uses **dedicated MetaApi paper/demo account**, never the live GFT 5k/10k Shan is trading on.
2. Read-only. The only MetaApi calls this slice makes are `get_open_positions()` and `get_account_state()`.
3. If MetaApi is unreachable, log error + return empty. **Do not write speculative data.**
4. No scheduling/daemon. This slice exposes a one-shot CLI script.

---

## 2. What to build

### 2.1 File: `Vanguard_QAenv/vanguard/execution/metaapi_client.py` (new)

```python
class MetaApiUnavailable(Exception): pass

class MetaApiClient:
    def __init__(self, account_id: str, api_token: str, base_url: str, timeout_s: int = 10):
        self.account_id = account_id
        self.api_token = api_token
        self.base_url = base_url
        self.timeout_s = timeout_s

    async def get_open_positions(self) -> list[dict]:
        """
        Returns list of dicts with keys:
        - broker_position_id (str)
        - symbol (str)
        - side (str: 'LONG' | 'SHORT')
        - qty (float)
        - entry_price (float)
        - current_sl (float | None)
        - current_tp (float | None)
        - opened_at_utc (str ISO)
        - unrealized_pnl (float)
        Raises MetaApiUnavailable on network/auth failure.
        """

    async def get_account_state(self) -> dict:
        """
        Returns dict with keys: equity, balance, margin, free_margin.
        Raises MetaApiUnavailable on failure.
        """
```

Implementation uses the official metaapi-cloud-sdk Python client. If the SDK is not installed: fail at import time with clear error, not silent stub.

### 2.2 DB table: `vanguard_open_positions` (new)

```sql
CREATE TABLE IF NOT EXISTS vanguard_open_positions (
  profile_id TEXT NOT NULL,
  broker_position_id TEXT NOT NULL,
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
```

### 2.3 CLI script: `Vanguard_QAenv/scripts/metaapi_fetch_once.py` (new)

One-shot. Usage:
```
python3 scripts/metaapi_fetch_once.py --profile-id gft_10k_qa
```

Behavior:
1. Load runtime config, read `execution.bridges.metaapi_{profile_id}` block.
2. Instantiate MetaApiClient.
3. Call `get_open_positions()` and `get_account_state()`.
4. On success: upsert rows into `vanguard_open_positions`, print results as JSON table.
5. On MetaApiUnavailable: print error to stderr, exit code 2, **write nothing to DB**.

### 2.4 DB upsert helper: `Vanguard_QAenv/vanguard/execution/open_positions_writer.py` (new)

```python
def upsert_open_positions(db_path: str, profile_id: str, positions: list[dict], synced_at_utc: str):
    """
    Replaces all rows for profile_id with the current broker truth.
    Transactional: DELETE WHERE profile_id=? then INSERT all, in one txn.
    Does nothing if positions is None (outage case — keeps stale data with old timestamp).
    """
```

**Important:** if `positions == []` (broker has no open positions), delete all rows for that profile. If `positions is None` (outage), do nothing.

---

## 3. Acceptance tests

### Test 1 — SDK installed
```bash
python3 -c "from metaapi_cloud_sdk import MetaApi; print('ok')"
```
**Expect:** prints "ok". If not installed, CC runs `pip install metaapi-cloud-sdk` first.

### Test 2 — Fetch from paper account, open positions table populates
Manually open 2 test trades on the MetaApi paper account (via MT5 UI or a one-off direct SDK call). Run:
```bash
python3 scripts/metaapi_fetch_once.py --profile-id gft_10k_qa
sqlite3 /Users/sjani008/SS/Vanguard_QAenv/data/vanguard_universe.db \
  "SELECT profile_id, symbol, side, qty, entry_price FROM vanguard_open_positions;"
```
**Expect:** 2 rows, matching the 2 manual trades.

### Test 3 — Closing a paper trade removes it from mirror
Close one of the 2 trades manually. Re-run `metaapi_fetch_once.py`.
**Expect:** only 1 row in `vanguard_open_positions`.

### Test 4 — Empty broker state clears mirror
Close the remaining trade. Re-run.
**Expect:** 0 rows in `vanguard_open_positions` for that profile.

### Test 5 — Outage keeps stale data
Break credentials (invalid api_token). Manually insert a fake row for testing. Re-run.
**Expect:** script prints error, exits 2, fake row **still present** in DB (stale data preserved with old timestamp).
Then restore credentials + clean up fake row.

### Test 6 — Timestamp freshness
After a successful run, check `last_synced_at_utc` on every row.
**Expect:** within 30 seconds of current UTC time.

### Test 7 — Schema enforces primary key
Attempt to insert two rows with same `(profile_id, broker_position_id)`. SQLite should enforce PK.

---

## 4. Report-back criteria

CC produces:
1. `metaapi_client.py`, `open_positions_writer.py`, `metaapi_fetch_once.py`, plus table DDL
2. Output of Tests 1–7 verbatim
3. Sample row from `vanguard_open_positions` as JSON
4. Proof that outage does NOT overwrite DB (show before/after row counts during Test 5)

---

## 5. Stop-the-line triggers

- SDK cannot be imported → stop, request Shan to fix env
- Outage case writes to DB → stop, this is a critical bug
- Closing a trade in broker doesn't reflect in mirror on next fetch → stop, diagnose
- Test 5 fails (outage wipes mirror) → stop
