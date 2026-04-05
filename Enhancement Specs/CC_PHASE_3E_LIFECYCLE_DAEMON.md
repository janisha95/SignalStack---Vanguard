# CC Phase 3e — Lifecycle Daemon (Scheduled Loop, No Auto-Close)

**Target env:** `Vanguard_QAenv`
**Est time:** 30–45 min
**Depends on:** Phase 3d PASS
**Risk level:** MEDIUM — runs continuously, but only does the already-tested read + reconcile work
**Prod-path touches:** NONE

---

## 0. Why this slice exists alone

Slices 3a, 3c, 3d built the pieces. This slice wraps them into a **scheduled background loop** that runs independently of the orchestrator. Every 60 seconds it fetches broker state, mirrors to DB, reconciles divergences.

**No auto-close in this slice.** That lives in 3f. This slice is just "run the already-tested components on a schedule."

---

## 1. Hard rules

1. QAenv only.
2. The daemon **wraps existing tested functions** — no new business logic here.
3. Must be runnable as a background process via `nohup`, with PID file + log file.
4. If one iteration fails, log the error and continue the loop. Do not crash.
5. MetaApi outage is handled gracefully: skip that profile this iteration, alert once.
6. `vanguard_account_state` is updated from broker equity (the Phase 2b seed row is replaced with real broker truth).

---

## 2. What to build

### 2.1 File: `Vanguard_QAenv/vanguard/execution/lifecycle_daemon.py` (new)

```python
import asyncio
import signal

class LifecycleDaemon:
    def __init__(self, config, db_path, interval_s=60):
        self.config = config
        self.db_path = db_path
        self.interval_s = interval_s
        self._shutdown = False
        self._clients = {}            # profile_id -> MetaApiClient
        self._outage_flags = {}       # profile_id -> bool (alert once)

    async def run(self):
        """Main loop. Runs forever until shutdown signal."""
        for profile in self._active_profiles():
            self._clients[profile["id"]] = self._build_client(profile)

        while not self._shutdown:
            cycle_start = datetime.utcnow()
            for profile in self._active_profiles():
                try:
                    await self._sync_profile(profile)
                except MetaApiUnavailable as e:
                    self._on_outage(profile["id"], e)
                except Exception as e:
                    log_exception(f"lifecycle_daemon.sync_profile failed for {profile['id']}", e)
                    # continue to next profile
            elapsed = (datetime.utcnow() - cycle_start).total_seconds()
            sleep_for = max(0, self.interval_s - elapsed)
            await asyncio.sleep(sleep_for)

    async def _sync_profile(self, profile):
        # 1. Fetch broker state (3a)
        client = self._clients[profile["id"]]
        broker_positions = await client.get_open_positions()
        broker_state = await client.get_account_state()

        now_utc = iso_utc_now()

        # 2. Update vanguard_open_positions mirror (3a's upsert_open_positions)
        upsert_open_positions(self.db_path, profile["id"], broker_positions, now_utc)

        # 3. Update vanguard_account_state from broker equity
        self._update_account_state(profile["id"], broker_state, now_utc)

        # 4. Detect divergences (3c)
        db_journal_open = load_journal_open(self.db_path, profile["id"])
        divergences = detect_divergences(self.db_path, profile["id"], broker_positions,
                                         load_open_positions(self.db_path, profile["id"]),
                                         db_journal_open)

        # 5. Resolve divergences (3d)
        if divergences:
            resolve_divergences(self.db_path, divergences, broker_positions)

        # 6. Clear outage flag on success
        self._outage_flags[profile["id"]] = False

    def _on_outage(self, profile_id, exc):
        if not self._outage_flags.get(profile_id):
            # First outage — alert once
            send_telegram(f"LIFECYCLE_METAAPI_UNAVAILABLE profile={profile_id}: {exc}")
            self._outage_flags[profile_id] = True
        log_error(f"MetaApi unavailable for {profile_id}: {exc}")

    def _update_account_state(self, profile_id, broker_state, now_utc):
        """
        UPDATE vanguard_account_state SET
          equity=broker_state['equity'],
          updated_at_utc=now_utc
        WHERE profile_id=?
        Note: daily_pnl_pct and trailing_dd_pct computation deferred — Phase 2b seeded zeros are fine for now.
        """

    def handle_shutdown(self, signum, frame):
        self._shutdown = True
```

### 2.2 File: `Vanguard_QAenv/scripts/start_lifecycle_daemon.sh` (new)

```bash
#!/bin/bash
set -euo pipefail
cd /Users/sjani008/SS/Vanguard_QAenv
LOG=/tmp/lifecycle_daemon.log
PID=/tmp/lifecycle_daemon.pid

if [ -f "$PID" ] && kill -0 "$(cat $PID)" 2>/dev/null; then
  echo "Daemon already running, PID $(cat $PID)"
  exit 1
fi

nohup python3 -m vanguard.execution.lifecycle_daemon --interval 60 > "$LOG" 2>&1 &
echo $! > "$PID"
echo "Lifecycle daemon started. PID=$(cat $PID) LOG=$LOG"
```

### 2.3 File: `Vanguard_QAenv/scripts/stop_lifecycle_daemon.sh` (new)

```bash
#!/bin/bash
PID=/tmp/lifecycle_daemon.pid
if [ ! -f "$PID" ]; then
  echo "No PID file found"
  exit 0
fi
kill -TERM "$(cat $PID)" 2>/dev/null || echo "Process not running"
rm -f "$PID"
echo "Daemon stopped"
```

### 2.4 File: `Vanguard_QAenv/vanguard/execution/__main__.py` or entrypoint

Enable `python3 -m vanguard.execution.lifecycle_daemon` invocation. Parse `--interval`, `--db-path` args. Wire `SIGTERM` → `daemon.handle_shutdown`.

### 2.5 Telegram alerts

- `LIFECYCLE_DAEMON_STARTED` — on clean startup
- `LIFECYCLE_DAEMON_STOPPED` — on clean shutdown
- `LIFECYCLE_METAAPI_UNAVAILABLE` — once per outage per profile (not per iteration)
- `LIFECYCLE_MANUAL_REVIEW_REQUIRED` — fired from 3d's DB_CLOSED_BROKER_OPEN resolution

---

## 3. Acceptance tests

### Test 1 — Daemon starts cleanly
```bash
bash scripts/start_lifecycle_daemon.sh
sleep 3
ps -p $(cat /tmp/lifecycle_daemon.pid)  # verify running
tail /tmp/lifecycle_daemon.log
```
**Expect:** process alive, log shows "LIFECYCLE_DAEMON_STARTED" and first iteration output.

### Test 2 — Iteration completes under interval
Let daemon run 5 iterations (5 min with interval=60). Check log.
**Expect:** 5 iterations logged, each completing in < 10s. No errors.

### Test 3 — open_positions mirror stays fresh
```sql
SELECT profile_id, MAX(last_synced_at_utc) FROM vanguard_open_positions GROUP BY profile_id;
```
**Expect:** timestamps within 90 seconds of current UTC.

### Test 4 — account_state updated from broker
```sql
SELECT profile_id, equity, updated_at_utc FROM vanguard_account_state;
```
**Expect:** equity matches broker paper account balance, updated_at fresh.

### Test 5 — Divergence created while daemon running gets resolved
With daemon running, manually open a new MT5 position. Wait ≤ 90 seconds.
**Expect:** reconciliation_log shows UNKNOWN_POSITION with resolved_action=JOURNAL_ROW_INSERTED. Journal now has `recon_*` row.

### Test 6 — Outage alert fires once
With daemon running, break MetaApi creds for one profile. Watch logs for 3 iterations.
**Expect:** single `LIFECYCLE_METAAPI_UNAVAILABLE` Telegram (not 3 messages). Log shows error each iteration.
Restore creds — verify outage flag clears (log says "recovered" or stops complaining).

### Test 7 — Multi-profile resilience
Break one profile's creds, leave other valid. Observe.
**Expect:** broken profile logs error each iteration (outage flagged once); valid profile continues syncing normally.

### Test 8 — Graceful shutdown
```bash
bash scripts/stop_lifecycle_daemon.sh
```
**Expect:** log shows "LIFECYCLE_DAEMON_STOPPED", process exits cleanly, PID file removed.

### Test 9 — Exception in one iteration doesn't kill daemon
Inject a temporary error (e.g., break a DB write path). Daemon should log exception and continue to next iteration.
**Expect:** log has the exception traceback, but daemon keeps running.
Remove injection.

### Test 10 — Daemon runs 10+ minutes without memory leak
Start daemon, monitor RSS every 2 min for 10 min.
**Expect:** RSS stable (± 10%), no monotonic growth.

---

## 4. Report-back criteria

CC produces:
1. `lifecycle_daemon.py`, start/stop scripts, entrypoint
2. Test 1–10 output verbatim
3. 10-minute sample of daemon log
4. Memory usage graph/table from Test 10
5. Confirmation: daemon never calls `close_position()` or `place_order()` (grep in daemon code)

---

## 5. Stop-the-line triggers

- Daemon crashes / doesn't auto-restart iteration after exception
- Outage alert fires per-iteration (spam)
- Memory grows unbounded
- Daemon makes any broker write call (it shouldn't yet)
- mirror table timestamps go stale (> 2× interval)
