# CC Phase 6 — QA to Prod Promotion Gate with Canary

**Target env:** `Vanguard_QAenv` → `Vanguard` (prod)
**Est time:** 30–45 min
**Depends on:** Phases 0, 2a, 2b, 3, 4, 5 all PASS
**Prod-path touches:** WRITES TO PROD (controlled, single-file copy + canary cycle)

---

## 0. Why this exists

Everything built in QA is worthless until it's safely running in prod. This phase defines the **promotion checklist + canary cutover** so we don't replay the TTP demo account blow-up or the S8 delete-157-rows mistake.

**Promotion criteria (Shantanu's explicit requirement):** orchestrator loop must work good in QA for **more than 5 clean cycles without breaking or changing fundamental logic** before cutover begins.

---

## 1. Hard rules

1. **Prod backup BEFORE any prod write.** `sqlite3 prod.db ".backup /tmp/prod_pre_phase6_backup.db"`. Zero exceptions.
2. **Canary first, full cutover second.** Canary = single profile (gft_10k only) for 3 cycles in prod, then evaluate. Only after canary PASS do the other profiles get promoted.
3. **Rollback script ready before cutover.** Written + tested in dry-run mode before the first prod write.
4. **No mixing of prod + QA binaries.** Prod runs prod code; QA runs QA code. No symlinks, no shared modules being swapped mid-cycle.
5. **Human approval gate between every step.** Shan says go or this script stops.

---

## 2. Promotion checklist (CC runs this as pre-flight)

All checks must PASS before any prod write. CC produces a report and stops if any FAIL.

### Check 1 — QA stability: 5 clean cycles
```sql
-- in QA DB
SELECT COUNT(DISTINCT cycle_ts_utc) FROM vanguard_resolved_universe_log
WHERE cycle_ts_utc > datetime('now', '-1 hour');
```
**PASS:** ≥ 5 rows and none of the last 5 cycles has any error in `vanguard_session_log`.

### Check 2 — QA phase acceptance tests all pass
Re-run the acceptance tests from Phases 2a, 2b, 3, 4, 5 in one script. All must pass.

### Check 3 — QA config_version is stable
The `config_version` hasn't changed in the last 5 cycles (no active tweaking mid-run).

### Check 4 — QA parity with own shadow log
For the last 3 QA cycles, shadow_execution_log entries should match trade_journal entries for approved candidates. No drift.

### Check 5 — MetaApi paper account has 0 open positions in QA
Don't promote with open QA paper trades — they'd orphan.

### Check 6 — Prod backup succeeded and is readable
```bash
sqlite3 /tmp/prod_pre_phase6_backup.db "SELECT COUNT(*) FROM vanguard_tradeable_portfolio;"
```
Returns a number, doesn't error.

---

## 3. Promotion workflow

### Step 1 — Backup prod
```bash
BACKUP_PATH="/tmp/prod_pre_phase6_backup_$(date +%Y%m%d_%H%M%S).db"
sqlite3 /Users/sjani008/SS/Vanguard/data/vanguard_universe.db ".backup $BACKUP_PATH"
ls -lh $BACKUP_PATH
```
Record path.

### Step 2 — Prepare rollback script
Create `Vanguard/scripts/rollback_phase6.sh`:
```bash
#!/bin/bash
# Restores prod DB from pre-phase6 backup
# Usage: ./rollback_phase6.sh <backup_path>
set -euo pipefail
BACKUP=$1
PROD_DB=/Users/sjani008/SS/Vanguard/data/vanguard_universe.db
# Stop prod orchestrator
pkill -f "vanguard_orchestrator.py" || true
# Restore backup
cp "$BACKUP" "$PROD_DB"
echo "Rolled back. Restart prod orchestrator manually."
```
Test in dry-run: copy backup to a temp path and run rollback against that.

### Step 3 — Copy QA code into prod (staged)
Use `rsync --dry-run` first to preview:
```bash
rsync -av --dry-run \
  --exclude 'data/' \
  --exclude 'config/vanguard_runtime.json' \
  --exclude 'logs/' \
  --exclude '*.pyc' \
  --exclude '__pycache__/' \
  /Users/sjani008/SS/Vanguard_QAenv/ \
  /Users/sjani008/SS/Vanguard/
```
Shan reviews dry-run output. If approved:
```bash
rsync -av [same flags without --dry-run]
```

**Note:** `config/vanguard_runtime.json` is NOT copied. Prod gets its own config authored manually. QA config is a template.

### Step 4 — Author prod runtime config
Copy QA template, but flip:
- `runtime.env`: `qa-shadow` → `prod`
- `runtime.resolved_universe_mode`: `observe` (start observe, flip to enforce after canary)
- Profile `is_active`: only `gft_10k` active, others `false` (canary)
- Point MetaApi `account_id` to the **real live GFT 10K account**

Save to `/Users/sjani008/SS/Vanguard/config/vanguard_runtime.json`.

### Step 5 — Canary: 3 prod cycles with gft_10k only, observe mode
Start prod orchestrator + lifecycle daemon. Let it run **3 cycles (15 min at 5-min interval)**.

### Step 6 — Canary evaluation
After 3 cycles, check:
1. All 3 cycles completed without errors
2. `vanguard_resolved_universe_log` shows only gft_10k-reachable symbols
3. V6 `vanguard_tradeable_portfolio` decisions for gft_10k look correct manually
4. If any trades were submitted: they hit MetaApi correctly, slippage logged, journal populated
5. Telegram notifications fired as expected
6. No reconciliation divergences (or only explainable ones)

**If any FAIL:** stop, run rollback, diagnose. Do not proceed to Step 7.

### Step 7 — Expand to all profiles, still observe mode
Flip `gft_5k.is_active=true` and `ttp_10k_swing.is_active=true`. Let orchestrator reload config (Phase 5 reload mechanism). Run 5 more cycles observe mode.

### Step 8 — Flip to enforce mode
Change `runtime.resolved_universe_mode` to `enforce`. Let reload. Run 5 cycles. Verify pipeline now processes only in-scope symbols (compare V2 symbol count before/after).

### Step 9 — Sign-off
CC produces final promotion report. Shan approves or rolls back.

---

## 4. Acceptance tests

### Test 1 — All checklist items pass
Run the §2 checklist. All 6 checks return PASS.

### Test 2 — Rollback script works (dry run)
```bash
cp /tmp/prod_pre_phase6_backup_*.db /tmp/test_restore.db
bash Vanguard/scripts/rollback_phase6.sh /tmp/test_restore.db  # against a fake target
# verify it would restore correctly
```

### Test 3 — Canary does not blow existing live trades
If Shan has any existing manual GFT 10K trades open, list them BEFORE canary start. After canary Step 6, verify those positions are still open, untouched, unmodified SL/TP.

### Test 4 — Observe mode actually observes
During Step 5, V1/V2 should still poll the full universe. Log should say "WOULD have filtered to N symbols" but not actually filter.

### Test 5 — Enforce mode actually enforces
After Step 8, V2 health row count should shrink to only gft_10k + gft_5k + ttp scope symbols.

### Test 6 — Config reload without restart
During Step 7 (profile flipping), orchestrator picks up new config on next cycle without restart. No orphaned state.

### Test 7 — Emergency kill works
At any point, Shan can stop orchestrator + daemon with one command. Write `scripts/emergency_stop.sh`:
```bash
#!/bin/bash
pkill -f "vanguard_orchestrator.py" || true
pkill -f "lifecycle_daemon" || true
echo "Stopped. MetaApi positions remain open - manage manually."
```

---

## 5. Report-back criteria

CC must produce:
1. Completed checklist with PASS/FAIL for each of 6 checks
2. rsync dry-run output (for review before real copy)
3. Prod backup path + size
4. Canary report after Step 6: all 6 evaluation items with evidence
5. Before/after V2 symbol count (proving enforce mode)
6. Final prod config_version
7. Telegram notification log during promotion

---

## 6. Non-goals (this phase)

- No code changes during promotion (QA code is frozen, copied as-is)
- No feature additions mid-promotion
- No multi-day gradual rollout (too risky with live accounts)

---

## 7. Stop-the-line triggers

- Any of the 6 checklist items FAIL
- Canary produces unexpected trades (trades that shouldn't have been approved)
- Reconciliation divergences appear for existing manual GFT trades
- Prod backup can't be verified readable
- MetaApi account_id confusion (QA account used in prod config or vice versa)

---

## 8. What this phase does NOT promote

- Sprint 2 MTF architecture (separate future phase)
- React admin UI (separate future phase)
- New asset classes beyond what QA has tested
